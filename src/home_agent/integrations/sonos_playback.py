from __future__ import annotations

import asyncio
import time
import threading
from dataclasses import dataclass
from time import sleep
from typing import Dict, List, Optional, Set

from home_agent.core.logging import get_logger

_log = get_logger(service="sonos_playback")

_RESUME_POLL_STEP = 1.0
_RESUME_POLL_TIMEOUT = 10.0
_RESUME_MAX_RETRIES = 3
_DONE_POLL_MAX_CONSECUTIVE_ERRORS = 6


class SonosPlayback:
    """
    Plays audio URLs on Sonos speakers with snapshot/restore support.

    Snapshots are held across successive play_url() calls so that back-to-back
    announcements don't wastefully restore and re-snapshot between each one.
    Call restore_all() after the last announcement in a batch to resume music.
    """

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
        self._held: Dict[str, _HeldSnapshot] = {}
        self._held_lock = threading.Lock()

    @property
    def has_held_snapshots(self) -> bool:
        with self._held_lock:
            return bool(self._held)

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
        fire_and_forget: bool = False,
    ) -> None:
        """
        Play on each configured target (coordinator-aware), in parallel with a limit.

        Acquires a snapshot on first call; subsequent calls reuse the held snapshot.
        Does NOT restore — caller must invoke restore_all() when the batch is done.

        If fire_and_forget=True, returns as soon as Sonos starts playing
        (does not wait for playback to finish or add tail padding).
        """
        targets = self._resolve_targets()
        if not targets:
            return

        sem = asyncio.Semaphore(max(1, int(concurrency)))
        loop = asyncio.get_running_loop()

        if fire_and_forget:
            async def run_one(item: "_ResolvedTarget") -> None:
                async with sem:
                    await loop.run_in_executor(
                        None,
                        self._start_playback_only,
                        item.device,
                        url,
                        volume if volume is not None else item.volume,
                        title,
                        item.member_volumes if volume is None else None,
                    )
        else:
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

    async def restore_all(self) -> None:
        """Restore all held speaker snapshots with robust retry logic."""
        with self._held_lock:
            held = dict(self._held)
            self._held.clear()
        if not held:
            return
        _log.info("restore_all_start", speakers=list(held.keys()))
        loop = asyncio.get_running_loop()
        await asyncio.gather(*(
            loop.run_in_executor(None, _restore_one, h)
            for h in held.values()
        ))

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _start_playback_only(
        self, spk, url: str, volume: Optional[int], title: str,
        member_volumes: Optional[Dict[str, int]] = None,
    ) -> None:
        """Start playback and return immediately once Sonos begins playing."""
        ip = getattr(spk, "ip_address", "unknown")
        self._acquire_snapshot(spk)
        try:
            spk.play_uri(url, title=title, start=True)
            if member_volumes:
                for member_ip, member_vol in member_volumes.items():
                    try:
                        self._SoCo(member_ip).volume = max(0, min(100, int(member_vol)))
                    except Exception:
                        pass
                coord_ip = getattr(spk, "ip_address", None)
                if coord_ip and coord_ip not in member_volumes:
                    try:
                        spk.volume = max(0, min(100, int(volume if volume is not None else self._default_volume)))
                    except Exception:
                        pass
            else:
                try:
                    spk.volume = max(0, min(100, int(volume if volume is not None else self._default_volume)))
                except Exception:
                    pass
            _wait_for_playing(spk, timeout_seconds=2.0)
            _log.info("fire_and_forget_started", speaker=ip)
        except Exception:
            _log.exception("fire_and_forget_failed", speaker=ip)
            raise

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
        ip = getattr(spk, "ip_address", "unknown")
        self._acquire_snapshot(spk)
        try:
            spk.play_uri(url, title=title, start=True)
            # Set volume AFTER play_uri to avoid Sonos group volume normalization
            # that some firmware versions trigger when starting a new transport.
            if member_volumes:
                for member_ip, member_vol in member_volumes.items():
                    clamped = max(0, min(100, int(member_vol)))
                    try:
                        member_spk = self._SoCo(member_ip)
                        member_spk.volume = clamped
                        _log.info("volume_set", speaker=member_ip, volume=clamped)
                    except Exception as e:
                        _log.warning("member_volume_failed", speaker=member_ip, volume=clamped, error=str(e))
                # Also set on coordinator if not already covered by member_volumes.
                coord_ip = getattr(spk, "ip_address", None)
                if coord_ip and coord_ip not in member_volumes:
                    coord_vol = int(volume if volume is not None else self._default_volume)
                    try:
                        spk.volume = max(0, min(100, coord_vol))
                        _log.info("volume_set_coordinator", speaker=coord_ip, volume=coord_vol)
                    except Exception as e:
                        _log.warning("coordinator_volume_failed", speaker=coord_ip, error=str(e))
            else:
                target_vol = int(volume if volume is not None else self._default_volume)
                try:
                    spk.volume = max(0, min(100, target_vol))
                    _log.info("volume_set", speaker=ip, volume=target_vol)
                except Exception as e:
                    _log.warning("volume_set_failed", speaker=ip, volume=target_vol, error=str(e))
            _wait_for_playing(spk, timeout_seconds=2.0)
            if expected_duration_seconds is not None and expected_duration_seconds > 0:
                sleep(max(0.2, float(expected_duration_seconds) + 0.75))
            else:
                _wait_for_done_or_timeout(spk, timeout_seconds=float(done_timeout_seconds))
            if tail_padding_seconds and tail_padding_seconds > 0:
                sleep(float(tail_padding_seconds))
        except Exception:
            _log.exception("play_url_failed", speaker=ip)
            raise

    def _acquire_snapshot(self, spk) -> None:
        """Take a snapshot if one isn't already held for this speaker.

        Skips snapshot entirely when the speaker is idle (not playing anything),
        since there's nothing to restore afterwards.
        """
        ip = getattr(spk, "ip_address", "unknown")
        with self._held_lock:
            if ip in self._held:
                _log.info("snapshot_reused", speaker=ip)
                return
        was_playing = _is_playing(spk)
        if not was_playing:
            with self._held_lock:
                self._held[ip] = _HeldSnapshot(snap=None, was_playing=False, device=spk, ip=ip)
            _log.info("snapshot_skipped_idle", speaker=ip)
            return
        snap = self._Snapshot(spk)
        snap.snapshot()
        with self._held_lock:
            if ip not in self._held:
                self._held[ip] = _HeldSnapshot(snap=snap, was_playing=was_playing, device=spk, ip=ip)
                _log.info("snapshot_acquired", speaker=ip, was_playing=was_playing)
            else:
                _log.info("snapshot_reused", speaker=ip)

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
            key = getattr(coord, "ip_address", None) or ip
            vol = self._speaker_volume_map.get(ip)
            if vol is None:
                vol = self._speaker_volume_map.get(str(key))
            if vol is None:
                vol = int(self._default_volume)
            vol = max(0, min(100, int(vol)))

            if key in seen:
                for t in out:
                    if t.key == str(key):
                        t.member_volumes[ip] = vol
                        break
                continue
            seen.add(key)
            member_vols: Dict[str, int] = {ip: vol}
            out.append(_ResolvedTarget(device=coord, volume=vol, key=str(key), member_volumes=member_vols))
        return out


# ------------------------------------------------------------------
# Data classes
# ------------------------------------------------------------------

@dataclass
class _HeldSnapshot:
    snap: object
    was_playing: bool
    device: object
    ip: str


@dataclass
class _ResolvedTarget:
    device: object
    volume: int
    key: str
    member_volumes: Dict[str, int]


# ------------------------------------------------------------------
# Restore logic (runs in worker threads)
# ------------------------------------------------------------------

def _restore_one(held: _HeldSnapshot) -> None:
    """Restore a single speaker snapshot with adaptive polling and retries."""
    ip = held.ip
    spk = held.device
    snap = held.snap
    was_playing = held.was_playing

    if snap is None:
        _log.info("snapshot_restore_skipped_idle", speaker=ip)
        try:
            spk.stop()
        except Exception:
            pass
        return

    try:
        snap.restore()
        _log.info("snapshot_restored", speaker=ip, was_playing=was_playing)
    except Exception:
        _log.exception("snapshot_restore_failed", speaker=ip)
        try:
            spk.stop()
        except Exception:
            pass
        return

    if not was_playing:
        return

    # Adaptive polling: wait for Sonos to resume on its own after restore.
    if _poll_for_playing(spk, timeout=_RESUME_POLL_TIMEOUT):
        _log.info("playback_resumed", speaker=ip)
        return

    # Playback didn't resume organically — retry spk.play() with backoff.
    for attempt in range(1, _RESUME_MAX_RETRIES + 1):
        _log.warning("playback_not_resumed", speaker=ip, attempt=attempt, max_retries=_RESUME_MAX_RETRIES)
        try:
            spk.play()
        except Exception as e:
            _log.warning("play_retry_failed", speaker=ip, attempt=attempt, error=str(e))
            sleep(2.0 * attempt)
            continue
        if _poll_for_playing(spk, timeout=5.0):
            _log.info("playback_resumed_after_retry", speaker=ip, attempt=attempt)
            return
        sleep(2.0 * attempt)

    _log.error("playback_resume_exhausted", speaker=ip, retries=_RESUME_MAX_RETRIES)


# ------------------------------------------------------------------
# Transport polling helpers
# ------------------------------------------------------------------

def _poll_for_playing(soco_device, timeout: float) -> bool:
    """Poll transport state until PLAYING. Returns True if reached."""
    step = _RESUME_POLL_STEP
    waited = 0.0
    while waited < timeout:
        try:
            info = soco_device.get_current_transport_info()
            state = (info or {}).get("current_transport_state") or ""
            if str(state).upper() == "PLAYING":
                return True
        except Exception:
            pass
        sleep(step)
        waited += step
    return False


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
    """Wait until Sonos stops playing using UPnP event subscription.

    Subscribes to avTransport events for instant notification when playback
    stops, instead of polling. Falls back to polling if subscription fails.
    """
    ip = getattr(soco_device, "ip_address", "?")
    sub = None
    try:
        sub = soco_device.avTransport.subscribe(auto_renew=False, requested_timeout=60)
        _log.info("event_sub_ok", speaker=ip)
    except Exception as e:
        _log.warning("event_sub_failed", speaker=ip, error=type(e).__name__,
                     detail=str(e)[:100])

    if sub is not None:
        try:
            from queue import Empty
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                remaining = max(0.1, deadline - time.monotonic())
                try:
                    event = sub.events.get(timeout=min(remaining, 2.0))
                    state = (event.variables or {}).get("transport_state", "")
                    if str(state).upper() not in ("PLAYING", "TRANSITIONING"):
                        _log.info("event_stopped", speaker=ip, state=state)
                        return
                except Empty:
                    pass
        except Exception as e:
            _log.warning("event_wait_error", speaker=ip, error=type(e).__name__)
        finally:
            try:
                sub.unsubscribe()
            except Exception:
                pass
        return

    # Fallback: poll transport state
    step = 0.5
    waited = 0.0
    consecutive_errors = 0
    while waited < timeout_seconds:
        try:
            info = soco_device.get_current_transport_info()
            state = (info or {}).get("current_transport_state") or ""
            consecutive_errors = 0
            if str(state).upper() not in ("PLAYING", "TRANSITIONING"):
                return
        except Exception:
            consecutive_errors += 1
            if consecutive_errors >= _DONE_POLL_MAX_CONSECUTIVE_ERRORS:
                _log.warning(
                    "done_poll_abort",
                    speaker=ip,
                    consecutive_errors=consecutive_errors,
                )
                return
        sleep(step)
        waited += step
