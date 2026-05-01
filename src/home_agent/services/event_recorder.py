from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from home_agent.bus.error_reporter import ErrorReporter
from home_agent.bus.mqtt_client import MqttClient
from home_agent.config import AppSettings
from home_agent.core.logging import configure_logging, get_logger
from home_agent.db import DbConnectInfo, DbManager


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
    reporter = ErrorReporter(mqttc=mqttc, service="event-recorder", base_topic=settings.mqtt.base_topic)
    reporter.start_heartbeat(interval_seconds=30.0)
    mqttc.subscribe(topic)
    log.info("mqtt_connected", host=settings.mqtt.host, port=settings.mqtt.port)
    log.info("subscribed", topic=topic)

    db = DbManager(
        conninfo=settings.db.conninfo,
        log_info=DbConnectInfo(host=settings.db.host, port=settings.db.port, dbname=settings.db.name, user=settings.db.user),
        connect_timeout_seconds=10.0,
        reconnect_max_wait_seconds=60.0,
    )
    db.ensure_connected()
    log.info("db_connected", host=db.log_info.host, db=db.log_info.dbname)

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
    last_insert_ok_at = 0.0
    last_insert_err_at = 0.0
    last_insert_err_kind: Optional[str] = None

    _BATCH_MAX = 100
    _FLUSH_INTERVAL_S = 1.0
    batch: list[tuple] = []

    async def _flush_batch() -> None:
        nonlocal last_insert_ok_at, last_insert_err_at, last_insert_err_kind
        if not batch:
            return
        to_flush = list(batch)
        batch.clear()

        def _do(conn) -> None:
            with conn.cursor() as cur:
                cur.executemany(insert_sql, to_flush)

        try:
            await loop.run_in_executor(None, lambda: db.run(_do, retries=1))
            stats["insert_ok"] += len(to_flush)
            last_insert_ok_at = loop.time()
        except Exception as exc:
            stats["insert_err"] += len(to_flush)
            last_insert_err_at = loop.time()
            last_insert_err_kind = "batch_insert_failed"
            log.exception("batch_insert_failed", batch_size=len(to_flush))
            reporter.report_error("batch_insert_failed", exc)

    async def _flush_loop() -> None:
        while True:
            await asyncio.sleep(_FLUSH_INTERVAL_S)
            try:
                await _flush_batch()
            except Exception:
                pass

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

    async def status_loop() -> None:
        """
        High-signal liveness output so we can quickly tell if the recorder is healthy.
        """
        nonlocal last_insert_ok_at, last_insert_err_at, last_insert_err_kind
        while True:
            await asyncio.sleep(10)
            now = loop.time()
            mqtt_stats = mqttc.stats()

            ok_age = round(now - last_insert_ok_at, 1) if last_insert_ok_at > 0 else None
            err_age = round(now - last_insert_err_at, 1) if last_insert_err_at > 0 else None

            log.info(
                "status",
                mqtt_connected=bool(mqtt_stats.get("connected", 0)),
                mqtt_queue_size=mqtt_stats.get("queue_size"),
                mqtt_queue_max=mqtt_stats.get("queue_maxsize"),
                mqtt_dropped_total=mqtt_stats.get("dropped_total"),
                db_connected=db.is_connected(),
                last_insert_ok_age_seconds=ok_age,
                last_insert_err_age_seconds=err_age,
                last_insert_err_kind=last_insert_err_kind,
                last_topic=stats.get("last_topic"),
                last_type=stats.get("last_type"),
            )

    reporter_task = asyncio.create_task(stats_reporter())
    status_task = asyncio.create_task(status_loop())
    flush_task = asyncio.create_task(_flush_loop())

    try:
        while True:
            msg = await mqttc.next_message()
            stats["seen"] += 1
            stats["last_topic"] = msg.topic

            # Skip ESPHome diagnostic sensor state messages (high-frequency, low-value)
            if "/sensor/" in msg.topic and msg.topic.endswith("/state"):
                continue

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
                payload_obj = {"ts": now.isoformat(), "type": "raw", "data": {"raw": msg.payload.decode("utf-8", "replace")}}
                typ = "raw"

            stats["last_type"] = typ
            payload_json = json.dumps(payload_obj, separators=(",", ":"))

            batch.append((ts, msg.topic, source, typ, event_id, trace_id, payload_json))
            if len(batch) >= _BATCH_MAX:
                await _flush_batch()
    finally:
        flush_task.cancel()
        try:
            await _flush_batch()
        except Exception:
            pass
        reporter_task.cancel()
        status_task.cancel()
        db.close()
        await mqttc.close()


def main() -> int:
    asyncio.run(run_event_recorder())
    return 0

