from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


try:  # Optional dependency
    from pythonping import ping  # type: ignore

    _HAS_PYTHONPING = True
except Exception:  # pragma: no cover - import guarded at runtime
    _HAS_PYTHONPING = False


@dataclass(frozen=True)
class InternetCheckResult:
    sent: int
    received: int
    loss_percent: float
    avg_latency_ms: Optional[float]
    min_latency_ms: Optional[float]
    max_latency_ms: Optional[float]


def run_internet_check(
    *,
    host: str,
    duration_seconds: float = 10.0,
    interval_seconds: float = 1.0,
    timeout_seconds: float = 1.0,
) -> InternetCheckResult:
    if not _HAS_PYTHONPING:
        raise RuntimeError("pythonping_not_installed")

    interval = max(0.1, float(interval_seconds))
    count = max(1, int(round(float(duration_seconds) / interval)))

    responses = ping(
        host,
        count=count,
        interval=interval,
        timeout=float(timeout_seconds),
        privileged=False,
    )

    rtts: list[float] = []
    received = 0
    for r in responses:
        if getattr(r, "success", False):
            received += 1
            ms = _response_ms(r)
            if ms is not None:
                rtts.append(ms)

    sent = len(responses)
    loss_percent = 100.0 * (sent - received) / max(1, sent)
    avg_ms = sum(rtts) / len(rtts) if rtts else None
    min_ms = min(rtts) if rtts else None
    max_ms = max(rtts) if rtts else None

    return InternetCheckResult(
        sent=sent,
        received=received,
        loss_percent=loss_percent,
        avg_latency_ms=avg_ms,
        min_latency_ms=min_ms,
        max_latency_ms=max_ms,
    )


def _response_ms(resp: object) -> Optional[float]:
    for attr in ("time_elapsed_ms", "rtt_ms"):
        v = getattr(resp, attr, None)
        if isinstance(v, (int, float)):
            return float(v)
    v = getattr(resp, "time_elapsed", None)
    if isinstance(v, (int, float)):
        return float(v) * 1000.0
    return None
