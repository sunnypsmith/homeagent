from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential


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

    @retry(wait=wait_exponential(min=1, max=10), stop=stop_after_attempt(3))
    async def chat(
        self,
        *,
        system: str,
        user: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
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
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

