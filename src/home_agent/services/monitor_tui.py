from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, Iterable, List, Optional, Set, Tuple

from zoneinfo import ZoneInfo

from rich.align import Align
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from home_agent.bus.envelope import make_event
from home_agent.bus.mqtt_client import MqttClient
from home_agent.config import AppSettings
from home_agent.core.logging import configure_logging, get_logger


def _parse_rfc3339(s: str) -> Optional[datetime]:
    """
    Parse RFC3339 timestamps produced by `envelope.now_rfc3339()`.
    """
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        # Example: 2026-01-26T11:33:03.945345Z
        v = s.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _fmt_age_seconds(dt: Optional[datetime]) -> str:
    if not dt:
        return "-"
    now = datetime.now(timezone.utc)
    age = max(0.0, (now - dt).total_seconds())
    if age < 60:
        return f"{int(age)}s"
    if age < 3600:
        return f"{int(age // 60)}m"
    return f"{int(age // 3600)}h"


def _fmt_duration_s(age_s: float) -> str:
    """
    Format a duration into s/m/h (e.g. 47s, 13m, 2h).
    """
    a = max(0.0, float(age_s))
    if a < 60:
        return f"{int(a)}s"
    if a < 3600:
        return f"{int(a // 60)}m"
    return f"{int(a // 3600)}h"


def _short(s: str, n: int = 80) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[: max(0, n - 1)] + "…"


@dataclass
class SourceStats:
    source: str
    last_seen_utc: Optional[datetime] = None
    last_type: str = "-"
    last_topic: str = "-"
    total: int = 0
    _seen_times: Deque[float] = field(default_factory=lambda: deque(maxlen=10_000))  # epoch seconds

    def mark(self, *, ts_utc: Optional[datetime], typ: str, topic: str) -> None:
        self.total += 1
        if ts_utc:
            self.last_seen_utc = ts_utc
        self.last_type = typ or "-"
        self.last_topic = topic or "-"
        self._seen_times.append(time.time())

    def rate_60s(self) -> float:
        now = time.time()
        # Drop older than 60s.
        while self._seen_times and (now - self._seen_times[0]) > 60.0:
            self._seen_times.popleft()
        return float(len(self._seen_times)) / 60.0


def _safe_run_lines(args: list[str], timeout_s: float = 1.5) -> Optional[list[str]]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout_s, check=False)  # nosec
        out = (p.stdout or "").strip()
        if not out:
            return []
        return out.splitlines()
    except Exception:
        return None


def _running_homeagent_commands() -> Tuple[int, int, list[str]]:
    """
    Returns:
      (home_running_count, home_zombie_count, lines)
    """
    if not shutil.which("ps"):
        return (0, 0, [])

    lines = _safe_run_lines(["ps", "-eo", "pid=,stat=,etime=,cmd="], timeout_s=1.5)
    if lines is None:
        return (0, 0, [])

    home: list[str] = []
    home_zombies = 0
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        parts = s.split()
        if len(parts) < 4:
            continue
        pid, stat, etime = parts[0], parts[1], parts[2]
        cmd = " ".join(parts[3:])
        if ("home-agent" in cmd) or ("uvicorn" in cmd):
            if "Z" in stat:
                home_zombies += 1
            else:
                home.append(f"{pid} {stat} {etime} {cmd}".strip())
    return (len(home), home_zombies, home)


def _detect_running_services(proc_lines: Iterable[str]) -> Set[str]:
    """
    Best-effort mapping from `ps` command lines to service names.
    """
    running: Set[str] = set()
    for ln in proc_lines:
        s = (ln or "").strip()
        if not s:
            continue
        # Subcommand appears after `home-agent`.
        if "home-agent" in s:
            try:
                after = s.split("home-agent", 1)[1].strip()
            except Exception:
                after = ""
            sub = (after.split() or [""])[0].strip()
            if sub:
                running.add(sub)
        # Uvicorn could indicate ui-gateway in some deployments.
        if "uvicorn" in s:
            running.add("ui-gateway")
    return running


def _services_status_line(
    *,
    by_source: Dict[str, SourceStats],
    running_services: Set[str],
    stale_after_seconds: int = 120,
) -> Text:
    """
    Create a compact status strip for common services.
    - Green: running + recently seen
    - Yellow: running but stale, or not running but recently seen
    - Red: not running and not seen recently
    """
    # Display label -> (source name to look for, home-agent subcommand)
    services = [
        ("event-rec", "event-recorder", "event-recorder"),
        ("time", "time-trigger", "time-trigger"),
        ("sonos", "sonos-gateway", "sonos-gateway"),
        ("ui", "ui-gateway", "ui-gateway"),
        ("wakeup", "wakeup-agent", "wakeup-agent"),
        ("brief", "morning-briefing-agent", "morning-briefing-agent"),
        ("chime", "hourly-chime-agent", "hourly-chime-agent"),
        ("fixed", "fixed-announcement-agent", "fixed-announcement-agent"),
        ("camect", "camect-agent", "camect-agent"),
        ("caseta", "caseta-agent", "caseta-agent"),
        ("camlight", "camera-lighting-agent", "camera-lighting-agent"),
    ]

    now_utc = datetime.now(timezone.utc)

    def _recent(src: str) -> bool:
        st = by_source.get(src)
        if not st or not st.last_seen_utc:
            return False
        try:
            age = (now_utc - st.last_seen_utc).total_seconds()
            return age <= float(stale_after_seconds)
        except Exception:
            return False

    out = Text()
    for i, (label, src, subcmd) in enumerate(services):
        is_running = subcmd in running_services
        is_recent = _recent(src)

        if is_running and is_recent:
            style = "green"
        elif is_running or is_recent:
            style = "yellow"
        else:
            style = "red"

        if i:
            out.append("  ", style="dim")
        out.append("●", style=style)
        out.append(f" {label}", style="bold")
    return out


def _build_services_table(stats: Dict[str, SourceStats], *, max_rows: int = 20) -> Table:
    t = Table(title="Services (by MQTT source)", expand=True, pad_edge=False)
    t.add_column("source", no_wrap=True)
    t.add_column("age", justify="right", width=4)
    t.add_column("rate", justify="right", width=6)
    t.add_column("total", justify="right", width=7)
    t.add_column("last type", overflow="fold")

    rows = list(stats.values())
    rows.sort(key=lambda s: (s.last_seen_utc or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    for s in rows[: max(1, int(max_rows))]:
        age = _fmt_age_seconds(s.last_seen_utc)
        rate = s.rate_60s()
        t.add_row(
            s.source,
            age,
            f"{rate:.2f}/s",
            str(s.total),
            _short(s.last_type, 42),
        )
    if not rows:
        t.add_row("-", "-", "-", "-", "No activity seen yet")
    return t


def _build_feed_table(feed: Iterable[Tuple[str, str, str, str]]) -> Table:
    t = Table(title="Recent activity", expand=True, pad_edge=False)
    t.add_column("age", width=4, justify="right")
    t.add_column("source", no_wrap=True, width=18)
    t.add_column("type", overflow="fold")
    t.add_column("note", overflow="fold")
    any_rows = False
    for age, source, typ, note in feed:
        any_rows = True
        t.add_row(age, source, _short(typ, 42), _short(note, 44))
    if not any_rows:
        t.add_row("-", "-", "No activity seen yet", "-")
    return t


def _build_process_panel() -> Panel:
    """
    Best-effort process summary (works on Linux).
    """
    if not shutil.which("ps"):
        return Panel(Text("ps not available", style="dim"), title="Processes", border_style="dim")

    # System-wide zombie count (cheap approximation from ps output).
    lines = _safe_run_lines(["ps", "-eo", "stat="], timeout_s=1.5)
    zombies = 0
    if lines is not None:
        for s in lines:
            if "Z" in (s or ""):
                zombies += 1

    home_count, home_zombies, home = _running_homeagent_commands()

    text = Text()
    text.append(f"home-agent/uvicorn processes: {home_count}\n", style="bold")
    text.append(
        f"defunct/zombies: {home_zombies} (home-agent)  {zombies} (system)\n",
        style="yellow" if (home_zombies or zombies) else "dim",
    )
    if home:
        text.append("\n")
        for ln in home[:15]:
            text.append(_short(ln, 120) + "\n")
        if len(home) > 15:
            text.append(f"... +{len(home) - 15} more\n", style="dim")
    return Panel(text, title="Processes", border_style="cyan")


def _build_db_panel(settings: AppSettings) -> Panel:
    """
    Best-effort DB activity view using Postgres (Timescale) `events` table.
    """
    try:
        import psycopg  # type: ignore
    except Exception:
        return Panel(Text("psycopg not available", style="dim"), title="DB", border_style="dim")

    try:
        conn = psycopg.connect(settings.db.conninfo, autocommit=True)
    except Exception:
        return Panel(Text("DB connect failed", style="dim"), title="DB", border_style="dim")

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  now() AS now_utc,
                  (SELECT max(ingested_at) FROM events) AS last_ingested_at,
                  (SELECT count(*) FROM events WHERE ingested_at > now() - interval '60 seconds') AS last_60s
                """
            )
            now_utc, last_ingested_at, last_60s = cur.fetchone()

            cur.execute(
                """
                SELECT ingested_at, topic, source, type
                FROM events
                ORDER BY ingested_at DESC
                LIMIT 6
                """
            )
            rows = cur.fetchall()
    except Exception:
        return Panel(Text("DB query failed", style="dim"), title="DB", border_style="dim")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # Header stats
    header = Text()
    last_age = "-"
    if last_ingested_at is not None:
        try:
            age_s = max(0.0, (now_utc - last_ingested_at).total_seconds())
            if age_s < 60:
                last_age = f"{int(age_s)}s"
            elif age_s < 3600:
                last_age = f"{int(age_s // 60)}m"
            else:
                last_age = f"{int(age_s // 3600)}h"
        except Exception:
            last_age = "-"
    header.append(f"last_ingest_age={last_age}  ", style="bold")
    header.append(f"events_last_60s={int(last_60s)}", style="bold")

    t = Table(expand=True, pad_edge=False)
    t.add_column("age", width=4, justify="right")
    t.add_column("topic", overflow="fold")
    t.add_column("source", no_wrap=True, width=16)
    t.add_column("type", overflow="fold")

    for ingested_at, topic, source, typ in rows or []:
        age = "-"
        try:
            age = _fmt_age_seconds(ingested_at)
        except Exception:
            age = "-"
        t.add_row(age, _short(str(topic), 30), _short(str(source), 16), _short(str(typ), 24))

    return Panel(Group(header, Text(""), t), title="DB activity", border_style="green")


def _fetch_db_activity(settings: AppSettings) -> Optional[dict[str, Any]]:
    """
    Returns DB activity summary or None if DB unavailable.
    """
    try:
        import psycopg  # type: ignore
    except Exception:
        return None

    try:
        conn = psycopg.connect(settings.db.conninfo, autocommit=True)
    except Exception:
        return None

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  now() AS now_utc,
                  (SELECT max(ingested_at) FROM events) AS last_ingested_at,
                  (SELECT count(*) FROM events WHERE ingested_at > now() - interval '60 seconds') AS last_60s
                """
            )
            now_utc, last_ingested_at, last_60s = cur.fetchone()

            cur.execute(
                """
                SELECT ingested_at, topic, source, type
                FROM events
                ORDER BY ingested_at DESC
                LIMIT 6
                """
            )
            rows = cur.fetchall()

            # Error-ish events (best-effort patterns).
            cur.execute(
                """
                SELECT ingested_at, topic, source, type
                FROM events
                WHERE
                  type ILIKE '%%.failed%%'
                  OR type ILIKE '%%.error%%'
                  OR type ILIKE '%%.exception%%'
                  OR type ILIKE '%%.err%%'
                ORDER BY ingested_at DESC
                LIMIT 6
                """
            )
            err_rows = cur.fetchall()
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass

    last_ingest_age_s: Optional[float] = None
    if last_ingested_at is not None:
        try:
            last_ingest_age_s = max(0.0, (now_utc - last_ingested_at).total_seconds())
        except Exception:
            last_ingest_age_s = None

    return {
        "now_utc": now_utc,
        "last_ingested_at": last_ingested_at,
        "last_ingest_age_s": last_ingest_age_s,
        "events_last_60s": int(last_60s) if last_60s is not None else 0,
        "rows": rows or [],
        "err_rows": err_rows or [],
    }


def _build_db_panel_from_data(db: Optional[dict[str, Any]]) -> Panel:
    if not db:
        return Panel(Text("DB unavailable", style="dim"), title="DB activity", border_style="dim")

    now_utc = db.get("now_utc")
    last_ingest_age_s = db.get("last_ingest_age_s")
    last_60s = int(db.get("events_last_60s") or 0)

    last_age = "-"
    if isinstance(last_ingest_age_s, (int, float)):
        age_s = float(last_ingest_age_s)
        if age_s < 60:
            last_age = f"{int(age_s)}s"
        elif age_s < 3600:
            last_age = f"{int(age_s // 60)}m"
        else:
            last_age = f"{int(age_s // 3600)}h"

    header = Text()
    header.append(f"last_ingest_age={last_age}  ", style="bold")
    header.append(f"events_last_60s={last_60s}", style="bold")

    t = Table(expand=True, pad_edge=False)
    t.add_column("age", width=4, justify="right")
    t.add_column("topic", overflow="fold")
    t.add_column("source", no_wrap=True, width=16)
    t.add_column("type", overflow="fold")

    for ingested_at, topic, source, typ in db.get("rows") or []:
        age = "-"
        if isinstance(ingested_at, datetime):
            age = _fmt_age_seconds(ingested_at)
        t.add_row(age, _short(str(topic), 30), _short(str(source), 16), _short(str(typ), 24))

    return Panel(Group(header, Text(""), t), title="DB activity", border_style="green")


def _build_alerts_panel(
    *,
    by_source: Dict[str, SourceStats],
    running_services: Set[str],
    home_zombies: int,
    mqtt_connected: bool,
    db: Optional[dict[str, Any]],
    stale_after_seconds: int = 120,
) -> Panel:
    """
    Alerts = computed health warnings + recent error-like DB events.
    """
    services = [
        ("event-recorder", "event-recorder"),
        ("time-trigger", "time-trigger"),
        ("sonos-gateway", "sonos-gateway"),
        ("ui-gateway", "ui-gateway"),
        ("wakeup-agent", "wakeup-agent"),
        ("morning-briefing-agent", "morning-briefing-agent"),
        ("hourly-chime-agent", "hourly-chime-agent"),
        ("fixed-announcement-agent", "fixed-announcement-agent"),
        ("camect-agent", "camect-agent"),
        ("caseta-agent", "caseta-agent"),
        ("camera-lighting-agent", "camera-lighting-agent"),
    ]

    now_utc = datetime.now(timezone.utc)

    def _age_s(src: str) -> Optional[float]:
        st = by_source.get(src)
        if not st or not st.last_seen_utc:
            return None
        try:
            return max(0.0, (now_utc - st.last_seen_utc).total_seconds())
        except Exception:
            return None

    alerts: List[Tuple[str, str]] = []

    if not mqtt_connected:
        alerts.append(("MQTT disconnected", "monitor is not receiving events"))

    if home_zombies > 0:
        alerts.append(("Defunct processes", f"{home_zombies} defunct home-agent processes"))

    if db and isinstance(db.get("last_ingest_age_s"), (int, float)):
        age_s = float(db["last_ingest_age_s"])
        if age_s > 120.0 and int(db.get("events_last_60s") or 0) == 0:
            alerts.append(("DB ingest stale", f"last_ingest_age={int(age_s)}s, events_last_60s=0"))

    for source, subcmd in services:
        running = subcmd in running_services
        age = _age_s(source)
        recent = (age is not None) and (age <= float(stale_after_seconds))
        if running and not recent:
            if age is None:
                alerts.append((f"{source}", "running but no recent events seen"))
            else:
                alerts.append((f"{source}", f"running but last event {int(age)}s ago"))
        if (not running) and recent:
            alerts.append((f"{source}", "events seen recently but process not running"))

    # Recent error-like DB events (if any).
    err_rows = (db or {}).get("err_rows") if db else []
    if err_rows:
        try:
            ingested_at, topic, source, typ = err_rows[0]
            if isinstance(ingested_at, datetime):
                alerts.append(("Recent error event", f"{_fmt_age_seconds(ingested_at)} {source} {typ}"))
        except Exception:
            pass

    t = Table(title="Alerts", expand=True, pad_edge=False)
    t.add_column("what", no_wrap=True, width=18)
    t.add_column("detail", overflow="fold")

    if not alerts:
        t.add_row("OK", "No alerts")
        return Panel(t, border_style="green")

    for what, detail in alerts[:10]:
        t.add_row(what, _short(detail, 70))
    return Panel(t, border_style="yellow")


def _build_top_topics_panel(topic_events: Deque[Tuple[float, str]]) -> Panel:
    """
    Top MQTT topics seen in the last 60 seconds.
    """
    now = time.time()
    # prune
    while topic_events and (now - topic_events[0][0]) > 60.0:
        topic_events.popleft()

    counts: Dict[str, int] = {}
    for _, topic in topic_events:
        counts[topic] = counts.get(topic, 0) + 1

    top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:10]

    t = Table(title="Top topics (60s)", expand=True, pad_edge=False)
    t.add_column("topic", overflow="fold")
    t.add_column("count", justify="right", width=6)
    t.add_column("rate", justify="right", width=7)

    if not top:
        t.add_row("-", "0", "0.00/s")
        return Panel(t, border_style="dim")

    for topic, c in top:
        t.add_row(_short(topic, 42), str(c), f"{(float(c) / 60.0):.2f}/s")
    return Panel(t, border_style="blue")


def _bootstrap_from_db(
    *,
    settings: AppSettings,
    by_source: Dict[str, SourceStats],
    feed: Deque[Tuple[float, str, str, str]],
    limit: int = 50,
) -> None:
    """
    Seed the top panels from the DB so the UI isn't empty on startup.
    """
    try:
        import psycopg  # type: ignore
    except Exception:
        return

    try:
        conn = psycopg.connect(settings.db.conninfo, autocommit=True)
    except Exception:
        return

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ingested_at, topic, source, type
                FROM events
                ORDER BY ingested_at DESC
                LIMIT %s
                """,
                (max(1, int(limit)),),
            )
            rows = cur.fetchall()
    except Exception:
        return
    finally:
        try:
            conn.close()
        except Exception:
            pass

    now_utc = datetime.now(timezone.utc)
    # Oldest-first so the feed ends up in correct order.
    for ingested_at, topic, source, typ in reversed(rows or []):
        src = str(source) if source is not None else "(unknown)"
        t = str(typ) if typ is not None else "(unknown)"
        ts_utc = ingested_at if isinstance(ingested_at, datetime) else None

        st = by_source.get(src)
        if st is None:
            st = SourceStats(source=src)
            by_source[src] = st
        st.mark(ts_utc=ts_utc, typ=t, topic=str(topic))

        age_s = 0.0
        if ts_utc is not None:
            try:
                age_s = max(0.0, (now_utc - ts_utc).total_seconds())
            except Exception:
                age_s = 0.0
        feed.append((time.time() - age_s, src, t, str(topic)))


async def run_monitor(*, topic: Optional[str] = None, refresh_seconds: float = 0.5, max_rows: int = 20) -> None:
    settings = AppSettings()
    configure_logging(settings.log_level)
    log = get_logger(service="monitor")

    sub_topic = (topic or "").strip() or f"{settings.mqtt.base_topic}/#"

    mqttc = MqttClient(
        host=settings.mqtt.host,
        port=settings.mqtt.port,
        username=settings.mqtt.username,
        password=settings.mqtt.password,
        client_id="homeagent-monitor",
        queue_maxsize=50_000,
    )
    await mqttc.connect()
    mqttc.subscribe(sub_topic)
    log.info("monitor_started", topic=sub_topic)

    by_source: Dict[str, SourceStats] = {}
    feed: Deque[Tuple[float, str, str, str]] = deque(maxlen=25)  # (seen_epoch, source, type, note)
    topic_events: Deque[Tuple[float, str]] = deque(maxlen=50_000)  # (seen_epoch, topic)
    _bootstrap_from_db(settings=settings, by_source=by_source, feed=feed, limit=50)

    # Self-test: publish a small ping so the top panels populate even on quiet systems.
    # Some brokers/configs may not deliver a client's own publishes back to itself,
    # so we use a second client id to publish.
    try:
        ping_topic = f"{settings.mqtt.base_topic}/monitor/ping"
        ping_evt = make_event(
            source="monitor",
            typ="monitor.ping",
            data={"ts_unix": int(time.time()), "subscribed": sub_topic},
        )
        pub = MqttClient(
            host=settings.mqtt.host,
            port=settings.mqtt.port,
            username=settings.mqtt.username,
            password=settings.mqtt.password,
            client_id="homeagent-monitor-pub",
            queue_maxsize=10,
        )
        await pub.connect()
        try:
            pub.publish_json(ping_topic, ping_evt)
        finally:
            await pub.close()
    except Exception:
        pass

    def _note_from_event(data: Any, topic_str: str) -> str:
        if isinstance(data, dict):
            if "text" in data and isinstance(data.get("text"), str):
                return f"text_len={len(data.get('text') or '')}"
            if "reason" in data and isinstance(data.get("reason"), str):
                return f"reason={data.get('reason')}"
        return topic_str

    async def _consume_loop() -> None:
        while True:
            msg = await mqttc.next_message()
            topic_events.append((time.time(), msg.topic))
            try:
                payload = msg.json()
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue

            src = payload.get("source")
            typ = payload.get("type")
            ts = payload.get("ts")
            data = payload.get("data")

            if not isinstance(src, str) or not src.strip():
                src = "(unknown)"
            if not isinstance(typ, str) or not typ.strip():
                typ = "(unknown)"

            ts_utc = _parse_rfc3339(ts) if isinstance(ts, str) else None

            st = by_source.get(src)
            if st is None:
                st = SourceStats(source=src)
                by_source[src] = st
            st.mark(ts_utc=ts_utc, typ=typ, topic=msg.topic)

            feed.append((time.time(), src, typ, _note_from_event(data, msg.topic)))

    consumer_task = asyncio.create_task(_consume_loop())

    # DB cache (avoid re-querying every render tick).
    db_cache: Optional[dict[str, Any]] = None
    db_cache_at = 0.0

    def _render() -> Group:
        nonlocal db_cache, db_cache_at
        now = time.time()
        feed_rows = []
        for seen_epoch, src, typ, note in list(feed)[::-1]:
            age_s = max(0.0, float(now - seen_epoch))
            age = _fmt_duration_s(age_s)
            feed_rows.append((age, src, typ, note))

        header = Table.grid(expand=True)
        header.add_column(justify="left")
        header.add_column(justify="right")
        now_local = datetime.now(tz=ZoneInfo(settings.timezone))
        header.add_row(
            Text("Home Agent Monitor", style="bold"),
            Text(
                f"subscribed: {sub_topic}\n{now_local.strftime('%Y-%m-%d %H:%M:%S %Z')}",
                style="dim",
            ),
        )

        mqtt = mqttc.stats()
        mqtt_connected = bool(mqtt.get("connected", 0))
        mqtt_line = Text(
            f"mqtt_connected={bool(mqtt.get('connected', 0))}  "
            f"queue={mqtt.get('queue_size')}/{mqtt.get('queue_maxsize')}  "
            f"received={mqtt.get('received_total')}  dropped={mqtt.get('dropped_total')}",
            style="green" if mqtt_connected else "yellow",
        )

        _, home_zombies, proc_lines = _running_homeagent_commands()
        running_services = _detect_running_services(proc_lines)
        svc_line = _services_status_line(by_source=by_source, running_services=running_services)

        # Refresh DB data at most every 2 seconds.
        if (now - db_cache_at) > 2.0:
            db_cache = _fetch_db_activity(settings)
            db_cache_at = now

        services_tbl = _build_services_table(by_source, max_rows=max_rows)
        recent_tbl = _build_feed_table(feed_rows[:25])

        top = Panel(
            Group(header, mqtt_line, svc_line),
            border_style="blue",
        )

        mid = Table.grid(expand=True)
        mid.add_column(ratio=1)
        mid.add_column(ratio=1)
        alerts_panel = _build_alerts_panel(
            by_source=by_source,
            running_services=running_services,
            home_zombies=home_zombies,
            mqtt_connected=mqtt_connected,
            db=db_cache,
        )
        topics_panel = _build_top_topics_panel(topic_events)

        proc_panel = _build_process_panel()
        db_panel = _build_db_panel_from_data(db_cache)

        left_col = Group(
            Panel(services_tbl, border_style="blue"),
            alerts_panel,
            proc_panel,
        )
        right_col = Group(
            Panel(recent_tbl, border_style="blue"),
            topics_panel,
            db_panel,
        )
        mid.add_row(left_col, right_col)

        help_line = Align.left(
            Text("Ctrl-C to exit. Shows MQTT activity by source + recent events.", style="dim")
        )

        return Group(top, mid, help_line)

    try:
        with Live(
            _render(),
            refresh_per_second=max(2, int(1.0 / max(0.1, refresh_seconds))),
            screen=True,
        ) as live:
            while True:
                # Re-render periodically so MQTT stats + tables update live.
                live.update(_render(), refresh=True)
                await asyncio.sleep(max(0.1, float(refresh_seconds)))
    except KeyboardInterrupt:
        pass
    finally:
        consumer_task.cancel()
        try:
            await mqttc.close()
        except Exception:
            pass


def main(*, topic: Optional[str] = None, refresh_seconds: float = 0.5, max_rows: int = 20) -> int:
    try:
        asyncio.run(run_monitor(topic=topic, refresh_seconds=refresh_seconds, max_rows=max_rows))
        return 0
    except KeyboardInterrupt:
        return 0

