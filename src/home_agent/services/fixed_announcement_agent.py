from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

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


async def run_fixed_announcement_agent() -> None:
    """
    Consume scheduled "fixed announcement" time events and emit announce.request.

    Expected incoming event:
      - type: time.cron.fixed_announcement
      - data.text: required
      - data.volume: optional
      - data.targets: optional list[str]
      - data.concurrency: optional int
    """
    settings = AppSettings()
    configure_logging(settings.log_level)
    log = get_logger(service="fixed_announcement_agent")

    mqttc = MqttClient(
        host=settings.mqtt.host,
        port=settings.mqtt.port,
        username=settings.mqtt.username,
        password=settings.mqtt.password,
        client_id="homeagent-fixed-announcement-agent",
    )
    await mqttc.connect()

    sub_topic = "%s/time/cron/fixed_announcement" % settings.mqtt.base_topic
    mqttc.subscribe(sub_topic)
    log.info("subscribed", topic=sub_topic)

    pub_topic = "%s/announce/request" % settings.mqtt.base_topic

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
                data = _require_dict(payload, "data")
            except Exception as e:
                log.warning("bad_event", topic=msg.topic, error=str(e))
                continue

            if typ != "time.cron.fixed_announcement":
                log.warning("unexpected_type", id=event_id, type=typ)
                continue

            text = str(data.get("text") or "").strip()
            if not text:
                log.warning("missing_text", id=event_id)
                continue

            announce_data: Dict[str, Any] = {"text": text}

            volume = data.get("volume")
            if isinstance(volume, int):
                announce_data["volume"] = int(volume)
            elif isinstance(volume, str) and volume.isdigit():
                announce_data["volume"] = int(volume)

            concurrency = data.get("concurrency")
            if isinstance(concurrency, int):
                announce_data["concurrency"] = int(concurrency)
            elif isinstance(concurrency, str) and concurrency.isdigit():
                announce_data["concurrency"] = int(concurrency)

            targets = data.get("targets")
            if isinstance(targets, list) and all(isinstance(x, str) for x in targets) and targets:
                announce_data["targets"] = list(targets)

            announce = make_event(
                source="fixed-announcement-agent",
                typ="announce.request",
                trace_id=trace_id,
                data=announce_data,
            )
            mqttc.publish_json(pub_topic, announce)
            log.info("published", to=pub_topic, trace_id=trace_id, from_event=event_id)
    finally:
        await mqttc.close()


def main() -> int:
    asyncio.run(run_fixed_announcement_agent())
    return 0

