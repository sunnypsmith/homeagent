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

    async def synthesize(
        self,
        *,
        text: str,
        voice_id: Optional[str] = None,
        output_format: Optional[str] = None,
    ) -> AudioBytes:
        if not self._api_key:
            raise RuntimeError("ELEVENLABS_API_KEY is not set")

        vid = voice_id or self._voice_id
        url = "%s/text-to-speech/%s" % (self._base_url, vid)

        headers: Dict[str, str] = {
            "xi-api-key": self._api_key,
            "accept": "audio/mpeg",
            "content-type": "application/json",
        }
        if output_format and output_format.startswith("wav"):
            headers["accept"] = "audio/wav"
        payload: Dict[str, Any] = {
            "text": text,
            # Defaults are fine to start; tune later.
            "model_id": "eleven_multilingual_v2",
        }

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            params = {"output_format": output_format} if output_format else None
            resp = await client.post(url, json=payload, headers=headers, params=params)
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
            content_type = resp.headers.get("content-type", "audio/mpeg")
            suggested_ext = "mp3"
            if output_format:
                of = output_format.lower()
                if of.startswith("wav"):
                    suggested_ext = "wav"
                elif of.startswith("pcm"):
                    suggested_ext = "wav"
            return AudioBytes(content_type=content_type, data=resp.content, suggested_ext=suggested_ext)

