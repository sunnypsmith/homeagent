"""Shared retry decorator for HTTP-based integrations."""
from __future__ import annotations

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

import httpx


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, ConnectionError, TimeoutError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500:
        return True
    return False


api_retry = retry(
    retry=retry_if_exception(_is_retryable),
    wait=wait_exponential(min=0.5, max=5),
    stop=stop_after_attempt(3),
    reraise=True,
)
