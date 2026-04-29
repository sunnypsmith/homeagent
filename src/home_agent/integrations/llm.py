from __future__ import annotations

import json as _json
import logging
import re
from typing import Any, Dict, List, Optional

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from dataclasses import dataclass

_log = logging.getLogger(__name__)

_FAILED_GEN_RE = re.compile(
    r"<function=(\w+)(.*?)</function>",
    re.DOTALL,
)


def _parse_failed_generation(error_body: dict) -> Optional["LLMToolCall"]:
    """Recover a tool call from Groq's failed_generation field.

    Llama models sometimes emit tool calls in XML format
    (<function=name{...}</function>) instead of using the structured API.
    Groq rejects these with a 400 but includes the raw generation.
    """
    fg = error_body.get("error", {}).get("failed_generation", "")
    if not fg:
        return None
    m = _FAILED_GEN_RE.search(fg)
    if not m:
        return None
    name = m.group(1)
    raw_args = m.group(2).strip()
    try:
        args = _json.loads(raw_args) if raw_args else {}
    except _json.JSONDecodeError:
        return None
    _log.info("recovered_tool_call from failed_generation: %s(%s)", name, args)
    return LLMToolCall(name=name, arguments=args)


def _is_retryable(exc: BaseException) -> bool:
    """Only retry on transient errors (5xx, timeouts, network). Never retry 4xx."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    return False


@dataclass
class LLMToolCall:
    """Returned when the LLM invokes a tool."""
    name: str
    arguments: Dict[str, Any]


@dataclass
class LLMTextResponse:
    """Returned when the LLM responds with plain text."""
    text: str


class LLMClient:
    """
    OpenAI-compatible Chat Completions client.
    Works with OpenAI and many self-hosted gateways that emulate /v1/chat/completions.
    """

    def __init__(self, *, base_url: str, api_key: Optional[str], model: str, timeout_seconds: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds

    @property
    def has_api_key(self) -> bool:
        return bool(self._api_key)

    @property
    def model_name(self) -> str:
        return self._model

    async def list_models(self) -> Optional[List[str]]:
        """
        Returns model ids from GET /models if supported by the provider.
        If endpoint is missing/blocked, returns None.
        """
        if not self._api_key:
            raise RuntimeError("LLM_API_KEY is not set")

        url = f"{self._base_url}/models"
        headers: Dict[str, str] = {"Authorization": "Bearer %s" % (self._api_key,)}

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code in (404, 405):
                return None
            resp.raise_for_status()
            data = resp.json()
            items = data.get("data", [])
            # OpenAI returns: {"data":[{"id":"..."}, ...]}
            return [m["id"] for m in items if isinstance(m, dict) and "id" in m]

    @retry(wait=wait_exponential(min=1, max=10), stop=stop_after_attempt(3),
           retry=retry_if_exception(_is_retryable))
    async def chat(
        self,
        *,
        system: str,
        user: str,
        max_tokens: Optional[int] = 1024,
        temperature: Optional[float] = 0.3,
    ) -> str:
        if not self._api_key:
            raise RuntimeError("LLM_API_KEY is not set")

        url = f"{self._base_url}/chat/completions"
        headers: Dict[str, str] = {"Authorization": "Bearer %s" % (self._api_key,)}
        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code >= 400:
                _log.warning("llm_error model=%s status=%d body=%s", self._model, resp.status_code, resp.text[:300])
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    @retry(wait=wait_exponential(min=1, max=10), stop=stop_after_attempt(3),
           retry=retry_if_exception(_is_retryable))
    async def chat_with_tools(
        self,
        *,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        max_tokens: Optional[int] = 1024,
        temperature: Optional[float] = 0.3,
    ) -> "LLMToolCall | LLMTextResponse":
        """Chat completions with OpenAI-compatible function/tool calling.

        Returns LLMToolCall if the model invokes a tool, LLMTextResponse otherwise.
        """
        if not self._api_key:
            raise RuntimeError("LLM_API_KEY is not set")

        url = f"{self._base_url}/chat/completions"
        headers: Dict[str, str] = {"Authorization": "Bearer %s" % (self._api_key,)}
        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code >= 400:
                _log.warning("llm_error model=%s status=%d body=%s", self._model, resp.status_code, resp.text[:300])
                if resp.status_code == 400:
                    recovered = _parse_failed_generation(resp.json())
                    if recovered:
                        return recovered
            resp.raise_for_status()
            data = resp.json()

        choice = data["choices"][0]
        msg = choice["message"]

        tool_calls = msg.get("tool_calls")
        if tool_calls and len(tool_calls) > 0:
            results = []
            for tc in tool_calls:
                args = tc["function"].get("arguments", "{}")
                if isinstance(args, str):
                    args = _json.loads(args)
                results.append(LLMToolCall(name=tc["function"]["name"], arguments=args))
            return results if len(results) > 1 else results[0]

        return LLMTextResponse(text=(msg.get("content") or "").strip())

