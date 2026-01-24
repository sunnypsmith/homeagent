from __future__ import annotations

import asyncio
import signal
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from home_agent.bus.envelope import make_event
from home_agent.bus.mqtt_client import MqttClient
from home_agent.config import AppSettings
from home_agent.core.logging import configure_logging, get_logger


@dataclass(frozen=True)
class ScheduleRow:
    id: int
    name: str
    enabled: bool
    kind: str
    timezone: str
    spec: str
    mqtt_topic: str
    event_type: str
    data: Dict[str, Any]


def _parse_interval(spec: str) -> Dict[str, int]:
    """
    Parse simple interval strings like: 60s, 5m, 1h
    """
    s = spec.strip().lower()
    if not s:
        raise ValueError("empty interval spec")
    unit = s[-1]
    n = int(s[:-1])
    if unit == "s":
        return {"seconds": n}
    if unit == "m":
        return {"minutes": n}
    if unit == "h":
        return {"hours": n}
    raise ValueError("unsupported interval unit (use s/m/h)")


def _parse_once(spec: str) -> datetime:
    s = spec.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise ValueError("once spec must include timezone offset or Z")
    return dt


def _parse_cron(spec: str) -> Tuple[str, str, str, str, str]:
    parts = spec.split()
    if len(parts) != 5:
        raise ValueError("cron spec must be 5 fields: 'min hour day month dow'")
    return parts[0], parts[1], parts[2], parts[3], parts[4]


async def run_time_trigger() -> None:
    settings = AppSettings()
    configure_logging(settings.log_level)
    log = get_logger(service="time_trigger")

    mqttc = MqttClient(
        host=settings.mqtt.host,
        port=settings.mqtt.port,
        username=settings.mqtt.username,
        password=settings.mqtt.password,
        client_id="homeagent-time-trigger",
    )
    await mqttc.connect()
    log.info("mqtt_connected", host=settings.mqtt.host, port=settings.mqtt.port)

    conn = psycopg.connect(settings.db.conninfo, autocommit=True)
    log.info("db_connected", host=settings.db.host, db=settings.db.name)

    scheduler = AsyncIOScheduler()
    scheduler.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _stop() -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass

    async def publish_schedule(s: ScheduleRow) -> None:
        # Merge schedule metadata into data, but keep user data nested/clean.
        data: Dict[str, Any] = dict(s.data or {})
        data.setdefault("schedule_id", s.id)
        data.setdefault("schedule_name", s.name)

        evt = make_event(source="time-trigger", typ=s.event_type, data=data)
        mqttc.publish_json(s.mqtt_topic, evt)

    def submit_publish(s: ScheduleRow) -> None:
        """
        APScheduler may execute jobs outside the asyncio loop context.
        Always submit the coroutine onto our running loop.
        """
        try:
            fut: Future = asyncio.run_coroutine_threadsafe(publish_schedule(s), loop)

            def _done(f: Future) -> None:
                try:
                    f.result()
                except Exception:
                    log.exception("publish_failed", schedule=s.name, topic=s.mqtt_topic, type=s.event_type)

            fut.add_done_callback(_done)
        except Exception:
            log.exception("publish_submit_failed", schedule=s.name, topic=s.mqtt_topic, type=s.event_type)

    def add_or_replace_job(s: ScheduleRow) -> None:
        job_id = "schedule:%d" % s.id
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass

        if not s.enabled:
            return

        if s.kind == "cron":
            minute, hour, day, month, dow = _parse_cron(s.spec)
            trigger = CronTrigger(
                minute=minute,
                hour=hour,
                day=day,
                month=month,
                day_of_week=dow,
                timezone=s.timezone,
            )
        elif s.kind == "interval":
            kwargs = _parse_interval(s.spec)
            trigger = IntervalTrigger(**kwargs, timezone=s.timezone)
        elif s.kind == "once":
            dt = _parse_once(s.spec)
            trigger = DateTrigger(run_date=dt)
        else:
            raise ValueError("unknown schedule kind: %r" % s.kind)

        scheduler.add_job(
            submit_publish,
            args=[s],
            trigger=trigger,
            id=job_id,
            name=s.name,
            replace_existing=True,
        )

    def load_schedules() -> List[ScheduleRow]:
        rows: List[ScheduleRow] = []
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, enabled, kind, timezone, spec, mqtt_topic, event_type, data
                FROM schedules
                ORDER BY id ASC
                """
            )
            for r in cur.fetchall():
                rows.append(
                    ScheduleRow(
                        id=int(r[0]),
                        name=str(r[1]),
                        enabled=bool(r[2]),
                        kind=str(r[3]),
                        timezone=str(r[4]),
                        spec=str(r[5]),
                        mqtt_topic=str(r[6]),
                        event_type=str(r[7]),
                        data=dict(r[8] or {}),
                    )
                )
        return rows

    async def reload_loop() -> None:
        """
        Simple v1: poll and re-register all schedules every 60 seconds.
        """
        while not stop_event.is_set():
            try:
                schedules = await loop.run_in_executor(None, load_schedules)
                # Replace all jobs based on current DB view.
                for s in schedules:
                    add_or_replace_job(s)
                log.info("schedules_loaded", count=len(schedules))
            except Exception:
                log.exception("schedules_reload_failed")
            await asyncio.sleep(60)

    reload_task = asyncio.create_task(reload_loop())

    try:
        await stop_event.wait()
    finally:
        reload_task.cancel()
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        await mqttc.close()


def main() -> int:
    asyncio.run(run_time_trigger())
    return 0

