from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class AudioBytes:
    content_type: str
    data: bytes
    suggested_ext: str


class TTSClient:
    async def synthesize(self, *, text: str, voice_id: Optional[str] = None) -> AudioBytes:
        raise NotImplementedError

