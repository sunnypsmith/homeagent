from __future__ import annotations

import asyncio
from typing import Any, Dict

from home_agent.bus.envelope import make_event
from home_agent.bus.mqtt_client import MqttClient
from home_agent.config import AppSettings
from home_agent.core.logging import configure_logging, get_logger


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


async def run_hourly_house_check_agent() -> None:
    """
    Stub v1.

    Listens for a time trigger and emits a placeholder house-check request event.
    A future "house-check service" can subscribe to the request topic and perform
    real API calls (cameras, locks, sensors, etc.).
    """
    settings = AppSettings()
    configure_logging(settings.log_level)
    log = get_logger(service="hourly_house_check_agent")

    mqttc = MqttClient(
        host=settings.mqtt.host,
        port=settings.mqtt.port,
        username=settings.mqtt.username,
        password=settings.mqtt.password,
        client_id="homeagent-hourly-house-check-agent",
    )
    await mqttc.connect()

    sub_topic = "%s/time/cron/hourly_house_check" % settings.mqtt.base_topic
    mqttc.subscribe(sub_topic)
    log.info("subscribed", topic=sub_topic)

    pub_topic = "%s/house/check/request" % settings.mqtt.base_topic

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

            if typ != "time.cron.hourly_house_check":
                log.warning("unexpected_type", id=event_id, type=typ)
                continue

            req = make_event(
                source="hourly-house-check-agent",
                typ="house.check.request",
                trace_id=trace_id,
                data={
                    "mode": "stub",
                    "checks": [],
                },
            )
            mqttc.publish_json(pub_topic, req)
            log.info("published", to=pub_topic, trace_id=trace_id, from_event=event_id)
    finally:
        await mqttc.close()


def main() -> int:
    asyncio.run(run_hourly_house_check_agent())
    return 0

