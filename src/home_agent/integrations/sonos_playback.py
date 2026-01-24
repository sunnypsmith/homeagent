from __future__ import annotations

import asyncio
from time import sleep
from typing import List, Optional, Set


class SonosPlayback:
    def __init__(self, *, speaker_ips: List[str], default_volume: int) -> None:
        try:
            from soco import SoCo  # type: ignore
            from soco.snapshot import Snapshot  # type: ignore
        except Exception as e:  # pragma: no cover
            raise RuntimeError("SoCo not installed. Run: pip install -e '.[sonos]'") from e

        self._SoCo = SoCo
        self._Snapshot = Snapshot
        self._speaker_ips = list(speaker_ips)
        self._default_volume = default_volume

    async def play_url(
        self,
        *,
        url: str,
        volume: Optional[int] = None,
        title: str = "Home Agent",
        concurrency: int = 3,
        tail_padding_seconds: float = 3.0,
    ) -> None:
        """
        v1: play on each configured target (coordinator-aware), in parallel with a limit.

        SoCo is synchronous/blocking; we run each target in a worker thread.
        """
        targets = self._resolve_targets()
        if not targets:
            return

        sem = asyncio.Semaphore(max(1, int(concurrency)))
        loop = asyncio.get_running_loop()

        async def run_one(spk) -> None:
            async with sem:
                await loop.run_in_executor(
                    None, self._play_url_blocking, spk, url, volume, title, float(tail_padding_seconds)
                )

        await asyncio.gather(*(run_one(spk) for spk in targets))

    def _play_url_blocking(
        self, spk, url: str, volume: Optional[int], title: str, tail_padding_seconds: float
    ) -> None:
        snap = self._Snapshot(spk)
        try:
            snap.snapshot()
            try:
                spk.volume = int(volume if volume is not None else self._default_volume)
            except Exception:
                pass
            spk.play_uri(url, title=title, start=True)
            _wait_for_playing(spk, timeout_seconds=2.0)
            _wait_for_done_or_timeout(spk, timeout_seconds=25.0)
            # Sonos can report "not playing" a fraction early; add a small grace delay
            # so the last words aren't clipped before we restore the snapshot.
            if tail_padding_seconds and tail_padding_seconds > 0:
                sleep(float(tail_padding_seconds))
        finally:
            try:
                snap.restore()
            except Exception:
                try:
                    spk.stop()
                except Exception:
                    pass

    def _resolve_targets(self) -> List[object]:
        """
        Resolve each IP to its current group coordinator (to avoid silent playback).
        De-duplicate coordinators while preserving order.
        """
        seen: Set[str] = set()
        out: List[object] = []
        for ip in self._speaker_ips:
            d = self._SoCo(ip)
            try:
                coord = d.group.coordinator
            except Exception:
                coord = d
            # Unique key: coordinator ip if available.
            key = getattr(coord, "ip_address", None) or ip
            if key in seen:
                continue
            seen.add(key)
            out.append(coord)
        return out


def _wait_for_playing(soco_device, timeout_seconds: float) -> None:
    step = 0.1
    waited = 0.0
    while waited < timeout_seconds:
        try:
            info = soco_device.get_current_transport_info()
            state = (info or {}).get("current_transport_state") or ""
            if str(state).upper() == "PLAYING":
                return
        except Exception:
            return
        sleep(step)
        waited += step


def _wait_for_done_or_timeout(soco_device, timeout_seconds: float) -> None:
    """
    Best-effort: wait until Sonos stops playing, otherwise timeout.
    """
    step = 0.5
    waited = 0.0
    while waited < timeout_seconds:
        try:
            info = soco_device.get_current_transport_info()
            state = (info or {}).get("current_transport_state") or ""
            if str(state).upper() not in ("PLAYING", "TRANSITIONING"):
                return
        except Exception:
            return
        sleep(step)
        waited += step

