from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import sleep
from typing import Dict, List, Optional, Set, Tuple


class SonosPlayback:
    def __init__(
        self,
        *,
        speaker_ips: List[str],
        default_volume: int,
        speaker_volume_map: Optional[Dict[str, int]] = None,
    ) -> None:
        try:
            from soco import SoCo  # type: ignore
            from soco.snapshot import Snapshot  # type: ignore
        except Exception as e:  # pragma: no cover
            raise RuntimeError("SoCo not installed. Run: pip install -e '.[sonos]'") from e

        self._SoCo = SoCo
        self._Snapshot = Snapshot
        self._speaker_ips = list(speaker_ips)
        self._default_volume = default_volume
        self._speaker_volume_map = dict(speaker_volume_map or {})

    async def play_url(
        self,
        *,
        url: str,
        volume: Optional[int] = None,
        title: str = "Home Agent",
        concurrency: int = 3,
        tail_padding_seconds: float = 3.0,
        expected_duration_seconds: Optional[float] = None,
        done_timeout_seconds: float = 300.0,
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

        async def run_one(item: "_ResolvedTarget") -> None:
            async with sem:
                member_vols = item.member_volumes if volume is None else None
                await loop.run_in_executor(
                    None,
                    self._play_url_blocking,
                    item.device,
                    url,
                    volume if volume is not None else item.volume,
                    title,
                    float(tail_padding_seconds),
                    float(expected_duration_seconds) if expected_duration_seconds is not None else None,
                    float(done_timeout_seconds),
                    member_vols,
                )

        await asyncio.gather(*(run_one(t) for t in targets))

    def _play_url_blocking(
        self,
        spk,
        url: str,
        volume: Optional[int],
        title: str,
        tail_padding_seconds: float,
        expected_duration_seconds: Optional[float],
        done_timeout_seconds: float,
        member_volumes: Optional[Dict[str, int]] = None,
    ) -> None:
        snap = self._Snapshot(spk)
        was_playing = _is_playing(spk)
        try:
            snap.snapshot()
            # Set volume on each individual member speaker (handles grouped speakers).
            if member_volumes:
                for member_ip, member_vol in member_volumes.items():
                    try:
                        member_spk = self._SoCo(member_ip)
                        member_spk.volume = max(0, min(100, int(member_vol)))
                    except Exception:
                        pass
            else:
                target_vol = int(volume if volume is not None else self._default_volume)
                try:
                    spk.volume = target_vol
                except Exception:
                    pass
            spk.play_uri(url, title=title, start=True)
            _wait_for_playing(spk, timeout_seconds=2.0)
            if expected_duration_seconds is not None and expected_duration_seconds > 0:
                # For known short clips (e.g., test tones), Sonos can keep reporting PLAYING
                # for a while. Sleeping is both faster and avoids long "done" polling.
                sleep(max(0.2, float(expected_duration_seconds) + 0.75))
            else:
                _wait_for_done_or_timeout(spk, timeout_seconds=float(done_timeout_seconds))
            # Sonos can report "not playing" a fraction early; add a small grace delay
            # so the last words aren't clipped before we restore the snapshot.
            if tail_padding_seconds and tail_padding_seconds > 0:
                sleep(float(tail_padding_seconds))
        finally:
            try:
                snap.restore()
                if was_playing:
                    # Give Sonos a moment to settle after restore, then verify playback.
                    sleep(5.0)
                    if not _is_playing(spk):
                        try:
                            spk.play()
                        except Exception:
                            pass
            except Exception:
                try:
                    spk.stop()
                except Exception:
                    pass

    def _resolve_targets(self) -> List[object]:
        """
        Resolve each IP to its current group coordinator (to avoid silent playback).
        De-duplicate coordinators while preserving order.
        Collect per-speaker volume overrides for all members to set individually.
        """
        seen: Set[str] = set()
        out: List[_ResolvedTarget] = []
        for ip in self._speaker_ips:
            d = self._SoCo(ip)
            try:
                coord = d.group.coordinator
            except Exception:
                coord = d
            # Unique key: coordinator ip if available.
            key = getattr(coord, "ip_address", None) or ip
            # Build per-speaker volume map for this speaker (even if coordinator already seen)
            vol = self._speaker_volume_map.get(ip)
            if vol is None:
                vol = self._speaker_volume_map.get(str(key))
            if vol is None:
                vol = int(self._default_volume)
            vol = max(0, min(100, int(vol)))

            if key in seen:
                # Coordinator already queued, but still record this member's volume
                for t in out:
                    if t.key == str(key):
                        t.member_volumes[ip] = vol
                        break
                continue
            seen.add(key)
            member_vols: Dict[str, int] = {ip: vol}
            out.append(_ResolvedTarget(device=coord, volume=vol, key=str(key), member_volumes=member_vols))
        return out


@dataclass
class _ResolvedTarget:
    device: object
    volume: int
    key: str
    member_volumes: Dict[str, int]


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


def _is_playing(soco_device) -> bool:
    try:
        info = soco_device.get_current_transport_info()
        state = (info or {}).get("current_transport_state") or ""
        return str(state).upper() == "PLAYING"
    except Exception:
        return False


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

