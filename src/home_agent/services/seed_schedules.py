from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import psycopg

from home_agent.config import AppSettings


@dataclass(frozen=True)
class SeedSchedule:
    name: str
    enabled: bool
    kind: str  # 'cron' | 'interval' | 'once'
    timezone: str
    spec: str
    mqtt_topic: str
    event_type: str
    data: Dict[str, Any]


def default_seed_schedules(*, timezone: str, base_topic: str) -> List[SeedSchedule]:
    """
    Opinionated v1 seed set (New York schedule rules discussed):
      - Wakeup call: 6:00am Mon-Fri, 7:00am Sat-Sun
      - Morning briefing: 7:00am Mon-Fri, 8:00am Sat-Sun

    All schedules publish to the existing agent subscription topics/types.
    """
    wake_topic = "%s/time/cron/wakeup_call" % base_topic
    wake_type = "time.cron.wakeup_call"

    brief_topic = "%s/time/cron/morning_briefing" % base_topic
    brief_type = "time.cron.morning_briefing"

    chime_topic = "%s/time/cron/hourly_chime" % base_topic
    chime_type = "time.cron.hourly_chime"

    return [
        SeedSchedule(
            name="wakeup_weekday_0600",
            enabled=True,
            kind="cron",
            timezone=timezone,
            spec="0 6 * * mon-fri",
            mqtt_topic=wake_topic,
            event_type=wake_type,
            data={"variant": "weekday"},
        ),
        SeedSchedule(
            name="wakeup_weekend_0700",
            enabled=True,
            kind="cron",
            timezone=timezone,
            spec="0 7 * * sat,sun",
            mqtt_topic=wake_topic,
            event_type=wake_type,
            data={"variant": "weekend"},
        ),
        SeedSchedule(
            name="morning_briefing_weekday_0700",
            enabled=True,
            kind="cron",
            timezone=timezone,
            spec="0 7 * * mon-fri",
            mqtt_topic=brief_topic,
            event_type=brief_type,
            data={"variant": "weekday"},
        ),
        SeedSchedule(
            name="morning_briefing_weekend_0800",
            enabled=True,
            kind="cron",
            timezone=timezone,
            spec="0 8 * * sat,sun",
            mqtt_topic=brief_topic,
            event_type=brief_type,
            data={"variant": "weekend"},
        ),
        # Hourly chime:
        # - Start 1 hour after morning briefing.
        # - Only run during non-quiet hours by constraining the hour range.
        #   Quiet hours: 9pm-5:50am (weekday), 9pm-6:50am (weekend).
        SeedSchedule(
            name="hourly_chime_weekday_8_to_20",
            enabled=True,
            kind="cron",
            timezone=timezone,
            spec="0 8-20 * * mon-fri",
            mqtt_topic=chime_topic,
            event_type=chime_type,
            data={"variant": "weekday"},
        ),
        SeedSchedule(
            name="hourly_chime_weekend_9_to_20",
            enabled=True,
            kind="cron",
            timezone=timezone,
            spec="0 9-20 * * sat,sun",
            mqtt_topic=chime_topic,
            event_type=chime_type,
            data={"variant": "weekend"},
        ),
    ]


def upsert_schedules(conn: psycopg.Connection, schedules: Sequence[SeedSchedule]) -> Tuple[int, List[str]]:
    """
    Upsert by name, so this command is safe to run repeatedly.

    Returns (count, names).
    """
    names: List[str] = []
    with conn.cursor() as cur:
        for s in schedules:
            cur.execute(
                """
                INSERT INTO schedules
                  (name, enabled, kind, timezone, spec, mqtt_topic, event_type, data)
                VALUES
                  (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (name) DO UPDATE SET
                  enabled    = EXCLUDED.enabled,
                  kind       = EXCLUDED.kind,
                  timezone   = EXCLUDED.timezone,
                  spec       = EXCLUDED.spec,
                  mqtt_topic = EXCLUDED.mqtt_topic,
                  event_type = EXCLUDED.event_type,
                  data       = EXCLUDED.data,
                  updated_at = now()
                """,
                (
                    s.name,
                    bool(s.enabled),
                    s.kind,
                    s.timezone,
                    s.spec,
                    s.mqtt_topic,
                    s.event_type,
                    json.dumps(s.data or {}),
                ),
            )
            names.append(s.name)
    return len(names), names


def seed_default_schedules(*, timezone: Optional[str] = None, dry_run: bool = False) -> List[SeedSchedule]:
    settings = AppSettings()
    tz = timezone or settings.timezone
    schedules = default_seed_schedules(timezone=tz, base_topic=settings.mqtt.base_topic)

    if dry_run:
        return schedules

    conn = psycopg.connect(settings.db.conninfo, autocommit=True)
    try:
        upsert_schedules(conn, schedules)
        return schedules
    finally:
        try:
            conn.close()
        except Exception:
            pass

