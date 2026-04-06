"""Shared retry decorator for HTTP-based integrations."""
from __future__ import annotations

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

import httpx


api_retry = retry(
    retry=retry_if_exception_type((
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.ConnectError,
        httpx.ReadError,
        ConnectionError,
        TimeoutError,
    )),
    wait=wait_exponential(min=0.5, max=5),
    stop=stop_after_attempt(3),
    reraise=True,
)
