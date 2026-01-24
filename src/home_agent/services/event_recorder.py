from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import psycopg

from home_agent.bus.mqtt_client import MqttClient
from home_agent.config import AppSettings
from home_agent.core.logging import configure_logging, get_logger


def _parse_ts(value: object) -> Optional[datetime]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    # Accept RFC3339 "Z"
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    # Require timezone awareness; otherwise treat as invalid.
    if dt.tzinfo is None:
        return None
    return dt


async def run_event_recorder() -> None:
    settings = AppSettings()
    configure_logging(settings.log_level)
    log = get_logger(service="event_recorder")

    topic = "%s/#" % settings.mqtt.base_topic

    mqttc = MqttClient(
        host=settings.mqtt.host,
        port=settings.mqtt.port,
        username=settings.mqtt.username,
        password=settings.mqtt.password,
        client_id="homeagent-event-recorder",
    )
    await mqttc.connect()
    mqttc.subscribe(topic)
    log.info("subscribed", topic=topic)

    conn = psycopg.connect(settings.db.conninfo, autocommit=True)
    log.info("db_connected", host=settings.db.host, db=settings.db.name)

    insert_sql = """
        INSERT INTO events (ts, topic, source, type, id, trace_id, payload)
        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
    """

    loop = asyncio.get_running_loop()

    stats = {
        "seen": 0,
        "insert_ok": 0,
        "insert_err": 0,
        "json_ok": 0,
        "json_err": 0,
        "last_topic": None,
        "last_type": None,
    }

    def insert_row(
        ts: datetime,
        mqtt_topic: str,
        source: Optional[str],
        typ: Optional[str],
        event_id: Optional[str],
        trace_id: Optional[str],
        payload_json: str,
    ) -> None:
        with conn.cursor() as cur:
            cur.execute(
                insert_sql,
                (
                    ts,
                    mqtt_topic,
                    source,
                    typ,
                    event_id,
                    trace_id,
                    payload_json,
                ),
            )

    async def stats_reporter() -> None:
        while True:
            await asyncio.sleep(60)
            log.info(
                "stats",
                seen=stats["seen"],
                insert_ok=stats["insert_ok"],
                insert_err=stats["insert_err"],
                json_ok=stats["json_ok"],
                json_err=stats["json_err"],
                last_topic=stats["last_topic"],
                last_type=stats["last_type"],
            )
            # reset counters, keep last_* for context
            stats["seen"] = 0
            stats["insert_ok"] = 0
            stats["insert_err"] = 0
            stats["json_ok"] = 0
            stats["json_err"] = 0

    reporter_task = asyncio.create_task(stats_reporter())

    try:
        while True:
            msg = await mqttc.next_message()
            stats["seen"] += 1
            stats["last_topic"] = msg.topic

            now = datetime.now(timezone.utc)
            payload_obj: Dict[str, Any]
            source = None
            typ = None
            event_id = None
            trace_id = None
            ts = now

            try:
                payload_obj = json.loads(msg.payload.decode("utf-8"))
                stats["json_ok"] += 1
                ts2 = _parse_ts(payload_obj.get("ts"))
                if ts2 is not None:
                    ts = ts2
                source = payload_obj.get("source") if isinstance(payload_obj.get("source"), str) else None
                typ = payload_obj.get("type") if isinstance(payload_obj.get("type"), str) else None
                event_id = payload_obj.get("id") if isinstance(payload_obj.get("id"), str) else None
                trace_id = payload_obj.get("trace_id") if isinstance(payload_obj.get("trace_id"), str) else None
            except Exception:
                stats["json_err"] += 1
                # Store non-JSON payloads too.
                payload_obj = {"ts": now.isoformat(), "type": "raw", "data": {"raw": msg.payload.decode("utf-8", "replace")}}
                typ = "raw"

            stats["last_type"] = typ
            payload_json = json.dumps(payload_obj, separators=(",", ":"))

            try:
                await loop.run_in_executor(None, insert_row, ts, msg.topic, source, typ, event_id, trace_id, payload_json)
                stats["insert_ok"] += 1
            except Exception:
                stats["insert_err"] += 1
                log.exception("insert_failed", topic=msg.topic)
    finally:
        reporter_task.cancel()
        try:
            conn.close()
        except Exception:
            pass
        await mqttc.close()


def main() -> int:
    asyncio.run(run_event_recorder())
    return 0

