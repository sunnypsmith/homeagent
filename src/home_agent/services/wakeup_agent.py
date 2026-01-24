from __future__ import annotations

import asyncio
from typing import Any, Dict

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


def _spoken_wind_unit(unit: str) -> str:
    u = (unit or "").strip().lower()
    if u in ("mph", "mi/h", "mp/h"):
        return "miles per hour"
    if u in ("kmh", "km/h"):
        return "kilometers per hour"
    if u in ("m/s", "ms"):
        return "meters per second"
    if u in ("kn", "kt", "kts", "knots"):
        return "knots"
    return unit or "miles per hour"


async def run_wakeup_agent() -> None:
    settings = AppSettings()
    configure_logging(settings.log_level)
    log = get_logger(service="wakeup_agent")

    mqttc = MqttClient(
        host=settings.mqtt.host,
        port=settings.mqtt.port,
        username=settings.mqtt.username,
        password=settings.mqtt.password,
        client_id="homeagent-wakeup-agent",
    )
    await mqttc.connect()

    sub_topic = "%s/time/cron/wakeup_call" % settings.mqtt.base_topic
    mqttc.subscribe(sub_topic)
    log.info("subscribed", topic=sub_topic)

    pub_topic = "%s/announce/request" % settings.mqtt.base_topic
    weather_client = None
    if settings.weather.provider == "open_meteo" and settings.weather.latitude and settings.weather.longitude:
        weather_client = OpenMeteoClient(
            latitude=settings.weather.latitude,
            longitude=settings.weather.longitude,
            units=settings.weather.units,
            timeout_seconds=settings.weather.timeout_seconds,
        )

    try:
        while True:
            msg = await mqttc.next_message()
            try:
                payload: Dict[str, Any] = msg.json()
                event_id = _require_str(payload, "id")
                _require_str(payload, "ts")
                source = _require_str(payload, "source")
                typ = _require_str(payload, "type")
                trace_id = _require_str(payload, "trace_id")
                data = _require_dict(payload, "data")
            except Exception as e:
                log.warning("bad_event", topic=msg.topic, error=str(e))
                continue

            if typ != "time.cron.wakeup_call":
                log.warning("unexpected_type", id=event_id, type=typ)
                continue

            variant = data.get("variant") if isinstance(data.get("variant"), str) else None
            weather_line = ""
            if weather_client is not None:
                try:
                    w = await weather_client.current()
                    parts = []
                    if w.temperature is not None:
                        parts.append("Outside it is %d degrees" % int(round(w.temperature)))
                    wind_unit = _spoken_wind_unit(w.wind_unit)
                    if w.wind_speed is not None:
                        parts.append("with wind %d %s" % (int(round(w.wind_speed)), wind_unit))
                    if w.wind_gusts is not None and w.wind_gusts >= (w.wind_speed or 0) + 5:
                        parts.append("gusting to %d %s" % (int(round(w.wind_gusts)), wind_unit))
                    if parts:
                        weather_line = " " + ", ".join(parts) + "."
                except Exception:
                    log.warning("weather_failed")

            if variant == "weekend":
                text = "Good morning Smith Family. It is seven A M. It is time to wake up.%s" % weather_line
            else:
                text = "Good morning Smith Family. It is six A M. It is time to wake up.%s" % weather_line

            announce = make_event(
                source="wakeup-agent",
                typ="announce.request",
                trace_id=trace_id,
                data={
                    "text": text,
                    # Let the gateway defaults handle volume/targets; override later if needed.
                },
            )
            mqttc.publish_json(pub_topic, announce)
            log.info("published", to=pub_topic, trace_id=trace_id, from_event=event_id, from_source=source)
    finally:
        await mqttc.close()


def main() -> int:
    asyncio.run(run_wakeup_agent())
    return 0

