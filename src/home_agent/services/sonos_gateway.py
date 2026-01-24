from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict

from zoneinfo import ZoneInfo

from home_agent.bus.envelope import make_event
from home_agent.bus.mqtt_client import MqttClient
from home_agent.config import AppSettings
from home_agent.core.logging import configure_logging, get_logger
from home_agent.integrations.audio_host import AudioHost
from home_agent.integrations.sonos_playback import SonosPlayback
from home_agent.integrations.tts_elevenlabs import ElevenLabsTTSClient


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
    player = SonosPlayback(speaker_ips=targets, default_volume=settings.sonos.default_volume)

    mqttc = MqttClient(
        host=settings.mqtt.host,
        port=settings.mqtt.port,
        username=settings.mqtt.username,
        password=settings.mqtt.password,
        client_id="homeagent-sonos-gateway",
    )
    await mqttc.connect()

    topic = "%s/announce/request" % settings.mqtt.base_topic
    mqttc.subscribe(topic)
    log.info("subscribed", topic=topic)

    tz = ZoneInfo(settings.timezone)
    suppressed_topic = "%s/announce/suppressed" % settings.mqtt.base_topic

    try:
        while True:
            msg = await mqttc.next_message()
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
            if typ != "announce.request":
                log.warning("bad_event", reason="unexpected_type", id=event_id, type=typ)
                continue
            if not (isinstance(trace_id, str) and trace_id):
                log.warning("bad_event", reason="missing_trace_id", id=event_id)
                continue
            if not isinstance(data, dict):
                log.warning("bad_event", reason="missing_data", id=event_id)
                continue

            text = str(data.get("text") or "").strip()
            if not text:
                log.warning("bad_event", reason="missing_text", id=event_id)
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

            data_targets = data.get("targets")
            play_targets = targets
            if isinstance(data_targets, list) and all(isinstance(x, str) for x in data_targets) and data_targets:
                play_targets = list(data_targets)

            log.info("announce_request", id=event_id, trace_id=trace_id, source=source)
            try:
                audio = await tts.synthesize(text=text, voice_id=voice_id)
                hosted = host.host_bytes(
                    data=audio.data,
                    filename="announce.%s" % audio.suggested_ext,
                    content_type=audio.content_type,
                    route_to_ip=play_targets[0],
                )
                player2 = (
                    player
                    if play_targets == targets
                    else SonosPlayback(speaker_ips=play_targets, default_volume=settings.sonos.default_volume)
                )
                await player2.play_url(
                    url=hosted.url,
                    volume=volume,
                    title="Home Agent",
                    concurrency=concurrency,
                )
                log.info("announce_done")
            except Exception:
                log.exception("announce_failed")
    finally:
        await mqttc.close()


def main() -> int:
    asyncio.run(run_sonos_gateway())
    return 0

