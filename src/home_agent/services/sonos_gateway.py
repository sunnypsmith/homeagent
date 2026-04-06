from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from zoneinfo import ZoneInfo

from home_agent.bus.envelope import make_event
from home_agent.bus.error_reporter import ErrorReporter
from home_agent.bus.mqtt_client import MqttClient
from home_agent.config import AppSettings
from home_agent.core.logging import configure_logging, get_logger
from home_agent.integrations.audio_host import AudioHost
from home_agent.integrations.sonos_playback import SonosPlayback
from home_agent.integrations.tts_elevenlabs import ElevenLabsTTSClient
from home_agent.offline_audio import OFFLINE_AUDIO_ITEMS


_TTS_NORMALIZE_PROMPT = (
    "You prepare text for a text-to-speech engine. Rewrite the input so it "
    "sounds natural when spoken aloud.\n\n"
    "Rules:\n"
    "- Expand ALL abbreviations: mph → miles per hour, F → Fahrenheit, "
    "NW → northwest, ft → feet, lbs → pounds, hrs → hours, min → minutes, "
    "govt → government, etc.\n"
    "- Spell out numbers as words: 42 → forty two, 3 → three.\n"
    "- Spell out times: 2:45 PM → two forty five P M.\n"
    "- Spell out currency: $1,500 → fifteen hundred dollars.\n"
    "- Spell out percentages: 22% → twenty two percent.\n"
    "- Fix broken punctuation or grammar only if obviously wrong.\n"
    "- Do NOT add, remove, or rephrase content. Keep the meaning identical.\n"
    "- Output ONLY the rewritten text, nothing else."
)


async def _normalize_for_tts(
    text: str, *, base_url: str, api_key: str, model: str, log,
) -> str:
    """Run text through a fast LLM to expand abbreviations and fix formatting."""
    if not api_key or not text.strip():
        return text
    try:
        headers = {"Authorization": "Bearer %s" % api_key, "Content-Type": "application/json"}
        payload = {
            "model": model,
            "max_tokens": 1024,
            "temperature": 0.0,
            "messages": [
                {"role": "system", "content": _TTS_NORMALIZE_PROMPT},
                {"role": "user", "content": text},
            ],
        }
        url = "%s/chat/completions" % base_url.rstrip("/")
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        result = (data["choices"][0]["message"]["content"] or "").strip()
        if result and len(result) > 10:
            log.info("tts_normalized", original_len=len(text), result_len=len(result))
            return result
    except Exception as e:
        log.warning("tts_normalize_failed", error=type(e).__name__)
    return text


def _parse_hhmm(s: str) -> int:
    """
    Parse "HH:MM" into minutes since midnight.
    """
    parts = (s or "").strip().split(":")
    if len(parts) != 2:
        raise ValueError("invalid_hhmm")
    hh = int(parts[0])
    mm = int(parts[1])
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        raise ValueError("invalid_hhmm")
    return hh * 60 + mm


def _is_quiet_now(*, now_local: datetime, weekday_start: str, weekday_end: str, weekend_start: str, weekend_end: str) -> bool:
    minute = now_local.hour * 60 + now_local.minute
    is_weekend = now_local.weekday() >= 5  # 5=Sat, 6=Sun

    start_s = weekend_start if is_weekend else weekday_start
    end_s = weekend_end if is_weekend else weekday_end
    start = _parse_hhmm(start_s)
    end = _parse_hhmm(end_s)

    if start == end:
        # Degenerate: treat as "always quiet".
        return True

    if start < end:
        # Quiet window does NOT cross midnight.
        return start <= minute < end

    # Quiet window crosses midnight.
    return minute >= start or minute < end


def _resolve_repo_path(raw: str) -> Path:
    p = Path(raw)
    if p.is_absolute():
        return p
    return Path(__file__).resolve().parents[3] / p


def _offline_audio_path(settings: AppSettings, key: str) -> Optional[Path]:
    for item in OFFLINE_AUDIO_ITEMS:
        if item["key"] == key:
            return _resolve_repo_path(settings.offline_audio.dir) / item["filename"]
    return None


async def run_sonos_gateway() -> None:
    settings = AppSettings()
    configure_logging(settings.log_level)
    log = get_logger(service="sonos_gateway")

    targets = settings.sonos.announce_target_ips
    if not targets:
        log.error("missing_sonos_targets", hint="Set SONOS_ANNOUNCE_TARGETS in .env")
        return

    tts = ElevenLabsTTSClient(
        api_key=settings.elevenlabs.api_key,
        voice_id=settings.elevenlabs.voice_id,
        base_url=settings.elevenlabs.base_url,
        timeout_seconds=settings.elevenlabs.timeout_seconds,
    )
    host = AudioHost()
    player = SonosPlayback(
        speaker_ips=targets,
        default_volume=settings.sonos.default_volume,
        speaker_volume_map=settings.sonos.speaker_volume_map,
    )

    mqttc = MqttClient(
        host=settings.mqtt.host,
        port=settings.mqtt.port,
        username=settings.mqtt.username,
        password=settings.mqtt.password,
        client_id="homeagent-sonos-gateway",
    )
    await mqttc.connect()
    log.info("mqtt_connected", host=settings.mqtt.host, port=settings.mqtt.port)

    reporter = ErrorReporter(mqttc=mqttc, service="sonos-gateway", base_topic=settings.mqtt.base_topic)
    reporter.start_heartbeat(interval_seconds=30.0)

    topic = "%s/announce/request" % settings.mqtt.base_topic
    mqttc.subscribe(topic)
    log.info("subscribed", topic=topic)

    tz = ZoneInfo(settings.timezone)
    suppressed_topic = "%s/announce/suppressed" % settings.mqtt.base_topic
    mute_topic = "%s/announce/mute" % settings.mqtt.base_topic
    mqttc.subscribe(mute_topic)
    hold_topic = "%s/sonos/hold" % settings.mqtt.base_topic
    mqttc.subscribe(hold_topic)
    log.info("subscribed", topic=mute_topic)

    loop = asyncio.get_running_loop()
    last_request_at = 0.0
    last_ok_at = 0.0
    last_err_at = 0.0
    last_err_kind: str | None = None
    suppressed_total = 0
    ok_total = 0
    err_total = 0
    muted_until_unix = 0
    pending_msg = None
    active_players: set = set()
    hold_active = False
    hold_until = 0.0

    async def _restore_active() -> None:
        nonlocal active_players
        for p in list(active_players):
            try:
                await p.restore_all()
            except Exception:
                log.exception("snapshot_restore_error")
        active_players.clear()

    async def status_loop() -> None:
        nonlocal last_request_at, last_ok_at, last_err_at, last_err_kind, muted_until_unix
        while True:
            await asyncio.sleep(10.0)
            now = loop.time()
            mqtt_stats = mqttc.stats()
            host_stats = host.stats()
            now_unix = int(datetime.now(timezone.utc).timestamp())
            muted_remaining_s = max(0, int(muted_until_unix) - now_unix) if muted_until_unix else 0
            if muted_until_unix and muted_remaining_s <= 0:
                # Avoid confusing "stale" mute timestamps in logs after expiry.
                muted_until_unix = 0
                log.info("mute_expired")

            req_age = round(now - last_request_at, 1) if last_request_at > 0 else None
            ok_age = round(now - last_ok_at, 1) if last_ok_at > 0 else None
            err_age = round(now - last_err_at, 1) if last_err_at > 0 else None

            log.info(
                "status",
                mqtt_connected=bool(mqtt_stats.get("connected", 0)),
                mqtt_queue_size=mqtt_stats.get("queue_size"),
                mqtt_queue_max=mqtt_stats.get("queue_maxsize"),
                mqtt_dropped_total=mqtt_stats.get("dropped_total"),
                # sonos-gateway does not connect to DB (event-recorder does)
                db_connected=None,
                announce_targets=len(targets),
                speaker_volume_overrides=len(settings.sonos.speaker_volume_map),
                quiet_hours_enabled=bool(settings.quiet_hours.enabled),
                muted=bool(muted_until_unix and muted_remaining_s > 0),
                muted_remaining_seconds=muted_remaining_s if muted_until_unix else None,
                muted_until_unix=int(muted_until_unix) if muted_until_unix else None,
                audio_host_started=bool(host_stats.get("started")),
                audio_host_base_url=host_stats.get("base_url"),
                audio_host_active_files=host_stats.get("active_files"),
                last_request_age_seconds=req_age,
                last_ok_age_seconds=ok_age,
                last_err_age_seconds=err_age,
                ok_total=ok_total,
                err_total=err_total,
                suppressed_total=suppressed_total,
                last_err_kind=last_err_kind,
            )

    status_task = asyncio.create_task(status_loop())

    try:
        while True:
            if pending_msg is not None:
                msg = pending_msg
                pending_msg = None
            else:
                # Expire stale holds (safety net)
                if hold_active and loop.time() > hold_until:
                    hold_active = False
                    log.info("sonos_hold_expired")

                if active_players and not hold_active:
                    await _restore_active()
                    _pb_done = make_event(source="sonos-gateway", typ="sonos.playback_done", data={})
                    mqttc.publish_json("%s/sonos/playback" % settings.mqtt.base_topic, _pb_done)
                msg = await mqttc.next_message()
            last_request_at = loop.time()
            try:
                payload: Dict[str, Any] = msg.json()
            except Exception:
                log.warning("bad_json", topic=msg.topic)
                continue

            # Strict envelope (no legacy payloads).
            event_id = payload.get("id")
            ts = payload.get("ts")
            source = payload.get("source")
            typ = payload.get("type")
            trace_id = payload.get("trace_id")
            data = payload.get("data")

            if not (isinstance(event_id, str) and event_id):
                log.warning("bad_event", reason="missing_id", topic=msg.topic)
                continue
            if not (isinstance(ts, str) and ts):
                log.warning("bad_event", reason="missing_ts", id=event_id)
                continue
            if not (isinstance(source, str) and source):
                log.warning("bad_event", reason="missing_source", id=event_id)
                continue
            if not (isinstance(typ, str) and typ):
                log.warning("bad_event", reason="missing_type", id=event_id)
                continue
            if not (isinstance(trace_id, str) and trace_id):
                log.warning("bad_event", reason="missing_trace_id", id=event_id)
                continue
            if not isinstance(data, dict):
                log.warning("bad_event", reason="missing_data", id=event_id)
                continue

            # Announce mute control. Intended to be published as a retained message so
            # it survives sonos-gateway restarts (broker replays retained on subscribe).
            if typ == "announce.mute":
                mtu = data.get("muted_until_unix")
                if isinstance(mtu, bool):
                    mtu = None
                if isinstance(mtu, str) and mtu.isdigit():
                    mtu = int(mtu)
                if isinstance(mtu, int):
                    muted_until_unix = max(0, int(mtu))
                    if muted_until_unix:
                        dt_utc = datetime.fromtimestamp(muted_until_unix, tz=timezone.utc)
                        dt_local = dt_utc.astimezone(tz)
                        log.warning(
                            "mute_set",
                            id=event_id,
                            trace_id=trace_id,
                            source=source,
                            muted_until_unix=muted_until_unix,
                            muted_until_utc=str(dt_utc),
                            muted_until_local=str(dt_local),
                        )
                    else:
                        log.info("mute_cleared", id=event_id, trace_id=trace_id, source=source)
                else:
                    log.warning("bad_event", reason="missing_muted_until_unix", id=event_id)
                continue

            if typ == "sonos.hold":
                action = data.get("action", "")
                if action == "start":
                    hold_active = True
                    hold_until = loop.time() + 30.0
                    log.info("sonos_hold_start", id=event_id, source=source)
                elif action == "release":
                    hold_active = False
                    if active_players:
                        await _restore_active()
                        _pb_done = make_event(source="sonos-gateway", typ="sonos.playback_done", data={})
                        mqttc.publish_json("%s/sonos/playback" % settings.mqtt.base_topic, _pb_done)
                    log.info("sonos_hold_release", id=event_id, source=source)
                continue

            if typ != "announce.request":
                log.warning("bad_event", reason="unexpected_type", id=event_id, type=typ)
                continue

            text = str(data.get("text") or "").strip()
            if not text:
                log.warning("bad_event", reason="missing_text", id=event_id)
                continue

            # Hard stop: never play anything while muted.
            if muted_until_unix:
                now_unix = int(datetime.now(timezone.utc).timestamp())
                if now_unix < int(muted_until_unix):
                    suppressed_total += 1
                    log.warning(
                        "announce_suppressed",
                        id=event_id,
                        trace_id=trace_id,
                        source=source,
                        reason="mute",
                        muted_until_unix=int(muted_until_unix),
                        local_time=str(datetime.now(tz=tz)),
                    )
                    suppressed = make_event(
                        source="sonos-gateway",
                        typ="announce.suppressed",
                        trace_id=trace_id,
                        data={
                            "reason": "mute",
                            "muted_until_unix": int(muted_until_unix),
                            "original_event_id": event_id,
                            "original_source": source,
                            "text_len": len(text),
                        },
                    )
                    mqttc.publish_json(suppressed_topic, suppressed)
                    continue

            # Hard stop: never play anything during quiet hours.
            if settings.quiet_hours.enabled:
                try:
                    now_local = datetime.now(tz=tz)
                    quiet = _is_quiet_now(
                        now_local=now_local,
                        weekday_start=settings.quiet_hours.weekday_start,
                        weekday_end=settings.quiet_hours.weekday_end,
                        weekend_start=settings.quiet_hours.weekend_start,
                        weekend_end=settings.quiet_hours.weekend_end,
                    )
                except Exception:
                    # Fail-safe: if quiet-hours config is malformed, assume quiet.
                    quiet = True

                if quiet:
                    suppressed_total += 1
                    log.warning(
                        "announce_suppressed",
                        id=event_id,
                        trace_id=trace_id,
                        source=source,
                        reason="quiet_hours",
                        local_time=str(datetime.now(tz=tz)),
                    )
                    suppressed = make_event(
                        source="sonos-gateway",
                        typ="announce.suppressed",
                        trace_id=trace_id,
                        data={
                            "reason": "quiet_hours",
                            "original_event_id": event_id,
                            "original_source": source,
                            "text_len": len(text),
                        },
                    )
                    mqttc.publish_json(suppressed_topic, suppressed)
                    continue

            voice_id = data.get("voice_id") if isinstance(data.get("voice_id"), str) else None
            volume = data.get("volume")
            concurrency_raw = data.get("concurrency")
            concurrency = settings.sonos.announce_concurrency
            if isinstance(concurrency_raw, int):
                concurrency = int(concurrency_raw)
            elif isinstance(concurrency_raw, str) and concurrency_raw.isdigit():
                concurrency = int(concurrency_raw)

            offline_key = data.get("offline_audio_key") if isinstance(data.get("offline_audio_key"), str) else None

            data_targets = data.get("targets")
            play_targets = targets
            if isinstance(data_targets, list) and all(isinstance(x, str) for x in data_targets) and data_targets:
                resolved = settings.sonos.resolve_targets(data_targets)
                if resolved:
                    play_targets = list(resolved)

            _t0 = loop.time()
            log.info("announce_request", id=event_id, trace_id=trace_id, source=source,
                     text_len=len(text), offline_key=offline_key or None)

            # Normalize text for TTS (expand abbreviations, spell out numbers)
            if not offline_key and settings.llm.api_key:
                text = await _normalize_for_tts(
                    text, base_url=settings.llm.base_url,
                    api_key=settings.llm.api_key, model=settings.llm.model, log=log)

            _t_norm = loop.time()

            # Notify voice service that speakers are about to play
            _pb_targets = data_targets if isinstance(data_targets, list) else list(settings.sonos.speaker_alias_map.keys())
            _pb_start = make_event(source="sonos-gateway", typ="sonos.playback_start", data={"targets": _pb_targets})
            mqttc.publish_json("%s/sonos/playback" % settings.mqtt.base_topic, _pb_start)
            try:
                hosted = None
                if offline_key:
                    path = _offline_audio_path(settings, offline_key)
                    if path and path.exists():
                        hosted = host.host_bytes(
                            data=path.read_bytes(),
                            filename=path.name,
                            content_type="audio/wav",
                            route_to_ip=play_targets[0],
                        )
                        log.info("announce_offline_audio", key=offline_key, path=str(path))

                _t_tts_start = loop.time()
                if hosted is None:
                    audio = await tts.synthesize(text=text, voice_id=voice_id)
                    hosted = host.host_bytes(
                        data=audio.data,
                        filename="announce.%s" % audio.suggested_ext,
                        content_type=audio.content_type,
                        route_to_ip=play_targets[0],
                    )
                _t_tts_done = loop.time()
                player2 = (
                    player
                    if play_targets == targets
                    else SonosPlayback(
                        speaker_ips=play_targets,
                        default_volume=settings.sonos.default_volume,
                        speaker_volume_map=settings.sonos.speaker_volume_map,
                    )
                )
                _t_play_start = loop.time()
                await player2.play_url(
                    url=hosted.url,
                    volume=volume,
                    title="Home Agent",
                    concurrency=concurrency,
                    tail_padding_seconds=float(settings.sonos.tail_padding_seconds),
                )
                _t_play_done = loop.time()
                active_players.add(player2)
                ok_total += 1
                last_ok_at = loop.time()
                log.info("announce_timing",
                         id=event_id, source=source,
                         normalize_ms=round((_t_norm - _t0) * 1000),
                         tts_ms=round((_t_tts_done - _t_tts_start) * 1000),
                         sonos_play_ms=round((_t_play_done - _t_play_start) * 1000),
                         total_ms=round((_t_play_done - _t0) * 1000),
                         offline=bool(offline_key))
                log.info("announce_done")

                # Publish playback_done after each announcement so voice service
                # can clear sonos_playing and eventually trigger hold release
                _pb_done = make_event(source="sonos-gateway", typ="sonos.playback_done", data={})
                mqttc.publish_json("%s/sonos/playback" % settings.mqtt.base_topic, _pb_done)

            except Exception as exc:
                played_fallback = False
                if offline_key:
                    try:
                        path = _offline_audio_path(settings, offline_key)
                        if path and path.exists():
                            hosted = host.host_bytes(
                                data=path.read_bytes(),
                                filename=path.name,
                                content_type="audio/wav",
                                route_to_ip=play_targets[0],
                            )
                            player2 = (
                                player
                                if play_targets == targets
                                else SonosPlayback(
                                    speaker_ips=play_targets,
                                    default_volume=settings.sonos.default_volume,
                                    speaker_volume_map=settings.sonos.speaker_volume_map,
                                )
                            )
                            await player2.play_url(
                                url=hosted.url,
                                volume=volume,
                                title="Home Agent",
                                concurrency=concurrency,
                                tail_padding_seconds=float(settings.sonos.tail_padding_seconds),
                            )
                            active_players.add(player2)
                            ok_total += 1
                            last_ok_at = loop.time()
                            log.info("announce_done_offline_fallback", key=offline_key, path=str(path))
                            played_fallback = True
                    except Exception:
                        pass
                if not played_fallback:
                    err_total += 1
                    last_err_at = loop.time()
                    last_err_kind = "announce_failed"
                    log.exception("announce_failed")
                    reporter.report_error("announce_failed", exc)

            # Peek for more messages to batch announcements.
            # During hold, use a longer peek (3s) to wait for the response announce
            # after the prompt. If nothing arrives, auto-release the hold.
            peek_timeout = 3.0 if hold_active else 0.5
            try:
                pending_msg = await asyncio.wait_for(mqttc.next_message(), timeout=peek_timeout)
                log.info("announce_batch_peek", queue_has_more=True)
            except asyncio.TimeoutError:
                if hold_active:
                    hold_active = False
                    if active_players:
                        await _restore_active()
                    _pb_done = make_event(source="sonos-gateway", typ="sonos.playback_done", data={})
                    mqttc.publish_json("%s/sonos/playback" % settings.mqtt.base_topic, _pb_done)
                    log.info("sonos_hold_auto_release", reason="no_more_announcements")
    finally:
        status_task.cancel()
        try:
            host.close()
        except Exception:
            pass
        await mqttc.close()


def main() -> int:
    asyncio.run(run_sonos_gateway())
    return 0

