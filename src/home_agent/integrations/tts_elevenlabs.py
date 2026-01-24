from __future__ import annotations

from typing import Any, Dict, Optional

import httpx

from home_agent.integrations.tts import AudioBytes, TTSClient


class ElevenLabsTTSClient(TTSClient):
    def __init__(
        self,
        *,
        api_key: Optional[str],
        voice_id: str,
        base_url: str,
        timeout_seconds: float,
    ) -> None:
        self._api_key = api_key
        self._voice_id = voice_id
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    async def synthesize(self, *, text: str, voice_id: Optional[str] = None) -> AudioBytes:
        if not self._api_key:
            raise RuntimeError("ELEVENLABS_API_KEY is not set")

        vid = voice_id or self._voice_id
        url = "%s/text-to-speech/%s" % (self._base_url, vid)

        headers: Dict[str, str] = {
            "xi-api-key": self._api_key,
            "accept": "audio/mpeg",
            "content-type": "application/json",
        }
        payload: Dict[str, Any] = {
            "text": text,
            # Defaults are fine to start; tune later.
            "model_id": "eleven_multilingual_v2",
        }

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                # Include the response body; ElevenLabs usually returns a helpful JSON error.
                body = ""
                try:
                    body = resp.text
                except Exception:
                    body = ""
                raise httpx.HTTPStatusError(
                    "%s body=%r" % (str(e), (body[:500] if body else "")),
                    request=e.request,
                    response=e.response,
                )
            return AudioBytes(content_type="audio/mpeg", data=resp.content, suggested_ext="mp3")

