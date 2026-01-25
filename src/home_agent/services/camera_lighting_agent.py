from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from zoneinfo import ZoneInfo

from home_agent.bus.envelope import make_event
from home_agent.bus.mqtt_client import MqttClient
from home_agent.config import AppSettings
from home_agent.core.logging import configure_logging, get_logger
from home_agent.integrations.weather_open_meteo import OpenMeteoClient


def _require_str(payload: Dict[str, Any], key: str) -> str:
    v = payload.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ValueError("missing_or_invalid_%s" % key)
    return v


def _require_dict(payload: Dict[str, Any], key: str) -> Dict[str, Any]:
    v = payload.get(key)
    if not isinstance(v, dict):
        raise ValueError("missing_or_invalid_%s" % key)
    return v


@dataclass
class _LightTimer:
    off_task: Optional[asyncio.Task]
    last_on_sent_at: float


def _normalize_detected_obj(v: object) -> str:
    if isinstance(v, str):
        return v.strip().lower()
    if isinstance(v, list):
        for x in v:
            if isinstance(x, str) and x.strip():
                return x.strip().lower()
    return ""


def _parse_token_list(s: str) -> set[str]:
    """
    Parse a comma/semicolon-delimited string into lowercased tokens.
    Example: "vehicle, car;person" -> {"vehicle","car","person"}
    """
    raw = (s or "").strip()
    if not raw:
        return set()
    out: set[str] = set()
    for chunk in raw.split(";"):
        for part in chunk.split(","):
            tok = part.strip().lower()
            if tok:
                out.add(tok)
    return out


def _expand_detected_obj_tokens(tokens: set[str]) -> set[str]:
    """
    Expand umbrella tokens to match common Camect labels.
    - vehicle -> car/truck/van/suv
    - person -> people/human
    """
    out: set[str] = set()
    for t in tokens:
        if t == "vehicle":
            out.update({"vehicle", "car", "truck", "van", "suv"})
        elif t in {"person", "people", "human"}:
            out.update({"person", "people", "human"})
        else:
            out.add(t)
    return out


def _as_tz(dt: Optional[datetime], tz: ZoneInfo) -> Optional[datetime]:
    if dt is None:
        return None
    # Open-Meteo often returns naive local datetimes.
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


async def run_camera_lighting_agent() -> None:
    settings = AppSettings()
    configure_logging(settings.log_level)
    log = get_logger(service="camera_lighting_agent")

    if not settings.camera_lighting.enabled:
        log.warning("camera_lighting_disabled", hint="Set CAMERA_LIGHTING_ENABLED=true to run")
        return

    if not settings.caseta.enabled:
        log.error("caseta_not_enabled", hint="Set CASETA_ENABLED=true and run caseta-agent")
        return

    if not (settings.weather.provider == "open_meteo" and settings.weather.latitude and settings.weather.longitude):
        log.error("missing_weather_location", hint="Set WEATHER_LAT and WEATHER_LON for dark detection")
        return

    tz = ZoneInfo(settings.timezone)

    weather = OpenMeteoClient(
        latitude=settings.weather.latitude,
        longitude=settings.weather.longitude,
        units=settings.weather.units,
        timeout_seconds=settings.weather.timeout_seconds,
    )

    # Cache sunrise/sunset.
    sunrise: Optional[datetime] = None
    sunset: Optional[datetime] = None
    sun_day: Optional[str] = None

    async def refresh_sun_times() -> None:
        nonlocal sunrise, sunset, sun_day
        try:
            st = await weather.sun_times_today()
            sunrise = _as_tz(st.sunrise, tz)
            sunset = _as_tz(st.sunset, tz)
            sun_day = datetime.now(tz=tz).date().isoformat()
            log.info(
                "sun_times_loaded",
                sunrise=str(sunrise) if sunrise else None,
                sunset=str(sunset) if sunset else None,
                tz=str(tz),
            )
        except Exception:
            log.exception("sun_times_failed")

    await refresh_sun_times()

    mqttc = MqttClient(
        host=settings.mqtt.host,
        port=settings.mqtt.port,
        username=settings.mqtt.username,
        password=settings.mqtt.password,
        client_id="homeagent-camera-lighting-agent",
    )
    await mqttc.connect()

    base = settings.mqtt.base_topic
    sub_topic = f"{base}/camera/event"
    cmd_topic = f"{base}/lutron/command"
    mqttc.subscribe(sub_topic)
    log.info("subscribed", topic=sub_topic)

    target_cam = (settings.camera_lighting.camera_name or "").strip()
    target_obj_raw = (settings.camera_lighting.detected_obj or "").strip()
    target_objs = _expand_detected_obj_tokens(_parse_token_list(target_obj_raw))
    device_id = (settings.camera_lighting.caseta_device_id or "").strip()
    duration = max(1, int(settings.camera_lighting.duration_seconds))
    min_retrigger = max(0, int(settings.camera_lighting.min_retrigger_seconds))

    timers: Dict[str, _LightTimer] = {}

    def is_dark_now() -> bool:
        if not settings.camera_lighting.only_dark:
            return True
        now = datetime.now(tz=tz)
        today = now.date().isoformat()
        # Refresh sunrise/sunset if day changed.
        if sun_day != today:
            # Fire-and-forget refresh; we’ll use last known values until updated.
            asyncio.create_task(refresh_sun_times())
        if sunrise is None or sunset is None:
            # Fail safe: don't turn on if we can't evaluate.
            return False
        # Dark if before sunrise or after sunset.
        return now < sunrise or now > sunset

    async def schedule_off(*, key: str) -> None:
        try:
            await asyncio.sleep(float(duration))
            evt = make_event(
                source="camera-lighting-agent",
                typ="lutron.command",
                data={"action": "off", "device_id": device_id},
            )
            mqttc.publish_json(cmd_topic, evt)
            log.info("lights_off", device_id=device_id, reason="timer_elapsed")
        finally:
            timers.pop(key, None)

    def trigger_lights(*, reason: str) -> None:
        now_mono = time.monotonic()
        key = device_id
        t = timers.get(key)

        # Extend timer: cancel existing off task.
        if t and t.off_task:
            try:
                t.off_task.cancel()
            except Exception:
                pass

        # Only send "on" if we haven't just sent one.
        should_send_on = True
        if t and (now_mono - float(t.last_on_sent_at)) < float(min_retrigger):
            should_send_on = False

        if should_send_on:
            evt = make_event(
                source="camera-lighting-agent",
                typ="lutron.command",
                data={"action": "on", "device_id": device_id},
            )
            mqttc.publish_json(cmd_topic, evt)
            log.info("lights_on", device_id=device_id, reason=reason)
            last_on = now_mono
        else:
            last_on = t.last_on_sent_at if t else now_mono

        off_task = asyncio.create_task(schedule_off(key=key))
        timers[key] = _LightTimer(off_task=off_task, last_on_sent_at=last_on)

    try:
        while True:
            msg = await mqttc.next_message()
            try:
                payload: Dict[str, Any] = msg.json()
                _require_str(payload, "id")
                _require_str(payload, "ts")
                _require_str(payload, "source")
                typ = _require_str(payload, "type")
                _require_str(payload, "trace_id")
                data = _require_dict(payload, "data")
            except Exception as e:
                log.warning("bad_event", topic=msg.topic, error=str(e))
                continue

            if typ != "camera.event":
                continue

            cam_name = str(data.get("camera_name") or "").strip()
            if cam_name != target_cam:
                continue

            evt_obj = ""
            evt_payload = data.get("event")
            if isinstance(evt_payload, dict):
                evt_obj = _normalize_detected_obj(evt_payload.get("detected_obj"))
            if target_objs and evt_obj not in target_objs:
                continue

            if not is_dark_now():
                log.info("ignored", reason="not_dark", camera=cam_name, detected_obj=evt_obj)
                continue

            trigger_lights(reason=f"{cam_name}:{evt_obj or 'event'}")
    finally:
        await mqttc.close()


def main() -> int:
    asyncio.run(run_camera_lighting_agent())
    return 0

