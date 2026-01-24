from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from time import perf_counter
from typing import Dict, List

import httpx
from home_agent.core.logging import get_logger
from home_agent.integrations.llm import LLMClient
from tenacity import RetryError


class CheckStatus(str, Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: CheckStatus
    details: str


async def run_startup_checks(*, llm: LLMClient) -> List[CheckResult]:
    """
    Runs fast preflight checks at process start.
    Keep these checks quick and side-effect-free.
    """
    log = get_logger(component="startup_checks")
    results: List[CheckResult] = []

    results.append(await _check_llm_provider(llm))

    # Summary
    counts: Dict[CheckStatus, int] = {s: sum(1 for r in results if r.status == s) for s in CheckStatus}
    log.info(
        "startup_checks_complete",
        ok=counts[CheckStatus.OK],
        warn=counts[CheckStatus.WARN],
        fail=counts[CheckStatus.FAIL],
    )
    for r in results:
        log.info("startup_check", name=r.name, status=r.status, details=r.details)

    return results


async def _check_llm_provider(llm: LLMClient) -> CheckResult:
    """
    1) verify API key present
    2) (optional) try /models to validate configured model id
    3) always hit the model with a tiny prompt and measure latency
    """
    name = "llm_provider"

    if not llm.has_api_key:
        return CheckResult(
            name=name,
            status=CheckStatus.FAIL,
            details="LLM_API_KEY is not set",
        )

    # Optional: /models is cheap; some providers don't support it.
    model_note = ""
    try:
        models = await llm.list_models()
        if models is None:
            model_note = " (/models not supported)"
        elif llm.model_name not in models:
            model_note = " (model not listed in /models)"
    except Exception:
        model_note = " (/models failed)"

    # Always probe the model itself, and measure speed.
    start = perf_counter()
    try:
        text = await llm.chat(
            system="You are a health check. Reply with exactly: PONG",
            user="PING",
            max_tokens=8,
            temperature=0.0,
        )
    except Exception as e:
        return CheckResult(
            name=name,
            status=CheckStatus.FAIL,
            details="Failed to reach LLM provider via chat: %s" % (_format_llm_error(e),),
        )
    latency_ms = int((perf_counter() - start) * 1000)

    expected = "PONG"
    if expected not in (text or "").upper():
        return CheckResult(
            name=name,
            status=CheckStatus.FAIL,
            details="LLM responded unexpectedly (%dms)%s" % (latency_ms, model_note),
        )

    # Speed gates (tune these once you see real-world latency).
    if latency_ms >= 8000:
        return CheckResult(
            name=name,
            status=CheckStatus.WARN,
            details="LLM is slow (%dms)%s" % (latency_ms, model_note),
        )

    return CheckResult(
        name=name,
        status=CheckStatus.OK,
        details="LLM OK (%dms)%s" % (latency_ms, model_note),
    )


def _format_llm_error(err: Exception) -> str:
    """
    Unwrap retries and include real HTTP status codes when available.
    """
    # tenacity wraps the final exception in RetryError; unwrap it.
    if isinstance(err, RetryError):
        last = getattr(err, "last_attempt", None)
        if last is not None:
            exc = last.exception()
            if exc is not None:
                return _format_llm_error(exc)
        return "RetryError (no last exception)"

    if isinstance(err, httpx.HTTPStatusError):
        status = err.response.status_code
        reason = err.response.reason_phrase
        url = str(err.request.url)
        # Body often includes structured error info; keep it short.
        body = ""
        try:
            txt = err.response.text or ""
            if txt:
                body = " body=%r" % (txt[:300],)
        except Exception:
            body = ""
        return "HTTP %d %s url=%s%s" % (status, reason, url, body)

    if isinstance(err, httpx.RequestError):
        # DNS/TLS/connect/timeout errors
        return "%s: %s" % (type(err).__name__, str(err))

    return "%s: %s" % (type(err).__name__, str(err) or "(no message)")

