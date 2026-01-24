from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class SonosSayRequest:
    text: str
    volume: Optional[int] = None


class SonosAnnouncer:
    async def say(self, req: SonosSayRequest) -> None:
        raise NotImplementedError


class StubSonosAnnouncer(SonosAnnouncer):
    async def say(self, req: SonosSayRequest) -> None:
        # Safe default: doesn't require extra deps and doesn't touch your speakers.
        print(f"[SONOS STUB] volume={req.volume} text={req.text}")


class SoCoMultiSonosAnnouncer(SonosAnnouncer):
    """
    Basic Sonos integration via SoCo.

    NOTE: Sonos generally plays a URL for announcements; true TTS usually requires
    generating audio and hosting it. This class is a starting point that you can
    extend (e.g., play_uri to a local TTS file/stream).
    """

    def __init__(self, speaker_ips: List[str], default_volume: int) -> None:
        try:
            from soco import SoCo  # type: ignore
        except Exception as e:  # pragma: no cover
            raise RuntimeError("SoCo not installed. Run: pip install -e '.[sonos]'") from e

        self._SoCo = SoCo
        self._speaker_ips = list(speaker_ips)
        self._default_volume = default_volume

    async def say(self, req: SonosSayRequest) -> None:
        # v1: treat targets individually (sequential).
        # NOTE: This is still a placeholder until we implement TTS->audio->play_uri.
        for ip in self._speaker_ips:
            spk = self._SoCo(ip)
            spk.volume = req.volume if req.volume is not None else self._default_volume
            print(f"[SONOS SoCo] ip={ip} volume={spk.volume} text={req.text}")

