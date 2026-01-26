from __future__ import annotations

import asyncio
import signal
import threading
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from home_agent.bus.envelope import make_event
from home_agent.bus.mqtt_client import MqttClient
from home_agent.config import AppSettings
from home_agent.core.logging import configure_logging, get_logger
from home_agent.db import DbConnectInfo, DbManager


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

    db = DbManager(
        conninfo=settings.db.conninfo,
        log_info=DbConnectInfo(host=settings.db.host, port=settings.db.port, dbname=settings.db.name, user=settings.db.user),
        connect_timeout_seconds=10.0,
        reconnect_max_wait_seconds=60.0,
    )
    db.ensure_connected()
    log.info("db_connected", host=db.log_info.host, db=db.log_info.dbname)

    scheduler = AsyncIOScheduler()
    scheduler.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _stop() -> None:
        # Visible marker that shutdown was requested (useful if we hang during cleanup).
        log.warning("shutdown_requested")
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
        # Debug visibility: show that the schedule was registered and when it will run next.
        try:
            job = scheduler.get_job(job_id)
            nrt = getattr(job, "next_run_time", None) if job is not None else None
            log.debug(
                "schedule_registered",
                schedule=s.name,
                job_id=job_id,
                kind=s.kind,
                spec=s.spec,
                timezone=s.timezone,
                next_run_time=str(nrt) if nrt is not None else None,
            )
        except Exception:
            pass

    def load_schedules() -> List[ScheduleRow]:
        def _do(conn) -> List[ScheduleRow]:
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

        return db.run(_do, retries=1)

    last_reload_started_at = 0.0
    last_reload_finished_at = 0.0
    reload_inflight = False
    last_schedules_count: int = 0
    last_schedules_enabled: int = 0
    last_schedules_sample: list[str] = []

    def load_schedules_daemon() -> "asyncio.Future[List[ScheduleRow]]":
        """
        Run a blocking DB fetch on a daemon thread.

        Why: asyncio's default executor uses non-daemon threads and `asyncio.run()`
        will wait for them at shutdown. If a DB call hangs, Ctrl-C can wedge the
        process. A daemon thread avoids that shutdown hang.
        """
        fut: "asyncio.Future[List[ScheduleRow]]" = loop.create_future()

        def _worker() -> None:
            try:
                rows = load_schedules()
            except Exception as e:
                loop.call_soon_threadsafe(fut.set_exception, e)
                return
            loop.call_soon_threadsafe(fut.set_result, rows)

        t = threading.Thread(target=_worker, daemon=True, name="time-trigger-db-load")
        t.start()
        return fut

    async def reload_loop() -> None:
        """
        Simple v1: poll and re-register all schedules every 60 seconds.
        """
        while not stop_event.is_set():
            try:
                nonlocal last_reload_started_at, last_reload_finished_at
                nonlocal reload_inflight
                nonlocal last_schedules_count, last_schedules_enabled, last_schedules_sample

                if reload_inflight:
                    log.warning("schedules_reload_skipped", reason="previous_reload_still_running")
                    # Sleep, but wake immediately on shutdown (Ctrl-C/SIGTERM).
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=10.0)
                    except asyncio.TimeoutError:
                        pass
                    continue

                reload_inflight = True
                last_reload_started_at = loop.time()
                # Load schedules in a worker thread (psycopg is blocking).
                schedules = await load_schedules_daemon()
                # Replace all jobs based on current DB view.
                for s in schedules:
                    add_or_replace_job(s)
                last_schedules_count = int(len(schedules))
                last_schedules_enabled = int(sum(1 for s in schedules if s.enabled))
                # Keep log output compact: sample a few names for visibility.
                last_schedules_sample = [s.name for s in schedules[:8]]
                log.info(
                    "schedules_loaded",
                    count=last_schedules_count,
                    enabled=last_schedules_enabled,
                    sample=last_schedules_sample,
                )
                last_reload_finished_at = loop.time()
                reload_inflight = False
            except Exception:
                reload_inflight = False
                log.exception("schedules_reload_failed")
            # Sleep, but wake immediately on shutdown (Ctrl-C/SIGTERM).
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=60.0)
            except asyncio.TimeoutError:
                pass

    reload_task = asyncio.create_task(reload_loop())

    async def status_loop() -> None:
        """
        Periodic liveness signal so we can tell whether the service is running or hung.
        """
        while not stop_event.is_set():
            await asyncio.sleep(10.0)
            st = mqttc.stats()
            now = loop.time()
            reload_age = None
            if last_reload_finished_at > 0:
                reload_age = round(now - last_reload_finished_at, 1)
            reload_runtime = None
            if last_reload_started_at > 0 and last_reload_finished_at < last_reload_started_at:
                reload_runtime = round(now - last_reload_started_at, 1)
            log.info(
                "status",
                mqtt_connected=bool(st.get("connected", 0)),
                mqtt_queue_size=st.get("queue_size"),
                mqtt_queue_max=st.get("queue_maxsize"),
                mqtt_dropped_total=st.get("dropped_total"),
                schedules_reload_inflight=bool(reload_inflight),
                schedules_loaded_count=last_schedules_count,
                schedules_enabled_count=last_schedules_enabled,
                schedules_last_reload_age_seconds=reload_age,
                schedules_reload_runtime_seconds=reload_runtime,
            )

    status_task = asyncio.create_task(status_loop())

    try:
        await stop_event.wait()
    finally:
        log.info("shutdown_start")
        reload_task.cancel()
        status_task.cancel()
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        log.info("shutdown_db_close")
        db.close()
        log.info("shutdown_mqtt_close")
        await mqttc.close()
        # If the process still doesn't exit, log remaining threads to help debug.
        try:
            threads = []
            for t in threading.enumerate():
                threads.append({"name": t.name, "daemon": bool(t.daemon), "alive": bool(t.is_alive())})
            log.info("shutdown_done", threads=threads)
        except Exception:
            log.info("shutdown_done")


def main() -> int:
    asyncio.run(run_time_trigger())
    return 0

