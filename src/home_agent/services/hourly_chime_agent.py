from __future__ import annotations

import asyncio
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


def _spoken_ampm(hour24: int) -> str:
    return "A M" if hour24 < 12 else "P M"


def _spoken_hour(hour24: int) -> str:
    h = hour24 % 12
    return "12" if h == 0 else str(h)


def _spoken_time_on_the_hour(dt: datetime) -> str:
    # v1: schedules fire on the hour; keep spoken string simple.
    return "%s %s" % (_spoken_hour(dt.hour), _spoken_ampm(dt.hour))


async def run_hourly_chime_agent() -> None:
    settings = AppSettings()
    configure_logging(settings.log_level)
    log = get_logger(service="hourly_chime_agent")

    mqttc = MqttClient(
        host=settings.mqtt.host,
        port=settings.mqtt.port,
        username=settings.mqtt.username,
        password=settings.mqtt.password,
        client_id="homeagent-hourly-chime-agent",
    )
    await mqttc.connect()

    sub_topic = "%s/time/cron/hourly_chime" % settings.mqtt.base_topic
    mqttc.subscribe(sub_topic)
    log.info("subscribed", topic=sub_topic)

    pub_topic = "%s/announce/request" % settings.mqtt.base_topic

    weather_client: Optional[OpenMeteoClient] = None
    if settings.weather.provider == "open_meteo" and settings.weather.latitude and settings.weather.longitude:
        weather_client = OpenMeteoClient(
            latitude=settings.weather.latitude,
            longitude=settings.weather.longitude,
            units=settings.weather.units,
            timeout_seconds=settings.weather.timeout_seconds,
        )

    tz = ZoneInfo(settings.timezone)

    try:
        while True:
            msg = await mqttc.next_message()
            try:
                payload: Dict[str, Any] = msg.json()
                event_id = _require_str(payload, "id")
                _require_str(payload, "ts")
                _require_str(payload, "source")
                typ = _require_str(payload, "type")
                trace_id = _require_str(payload, "trace_id")
                _require_dict(payload, "data")
            except Exception as e:
                log.warning("bad_event", topic=msg.topic, error=str(e))
                continue

            if typ != "time.cron.hourly_chime":
                log.warning("unexpected_type", id=event_id, type=typ)
                continue

            now_local = datetime.now(tz=tz)
            time_phrase = _spoken_time_on_the_hour(now_local)

            temp_phrase = ""
            if weather_client is not None:
                try:
                    w = await weather_client.current()
                    if w.temperature is not None:
                        temp_phrase = " Outside it is %d degrees." % int(round(w.temperature))
                except Exception:
                    log.warning("weather_failed")

            text = "Current time is %s.%s" % (time_phrase, temp_phrase)

            announce = make_event(
                source="hourly-chime-agent",
                typ="announce.request",
                trace_id=trace_id,
                data={"text": text},
            )
            mqttc.publish_json(pub_topic, announce)
            log.info("published", to=pub_topic, trace_id=trace_id, from_event=event_id)
    finally:
        await mqttc.close()


def main() -> int:
    asyncio.run(run_hourly_chime_agent())
    return 0

