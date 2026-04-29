"""Persistent intent store backed by TimescaleDB with pg_trgm fuzzy matching.

Replaces the JSON-file-based LearnedActionsStore with DB-backed storage that
survives container rebuilds and supports similarity-based phrase lookup.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from home_agent.core.logging import get_logger
from home_agent.db import DbManager

_log = get_logger(service="intent_store")

_SIMILARITY_THRESHOLD = 0.4


@dataclass
class LearnedIntent:
    id: int
    phrase: str
    category: str
    tool_name: str
    tool_args: Dict[str, Any]
    mqtt_topic: str
    mqtt_payload: Dict[str, Any]
    description: str
    use_count: int


class IntentStore:
    """DB-backed intent store with fuzzy phrase matching via pg_trgm."""

    def __init__(self, db: DbManager) -> None:
        self._db = db

    def find_match(self, phrase: str, threshold: float = _SIMILARITY_THRESHOLD) -> Optional[LearnedIntent]:
        """Find the best fuzzy match for a phrase. Returns None if below threshold."""
        def _query(conn):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, phrase, category, tool_name, tool_args,
                           mqtt_topic, mqtt_payload, description, use_count,
                           similarity(phrase, %s) AS sim
                    FROM learned_intents
                    WHERE similarity(phrase, %s) > %s
                    ORDER BY sim DESC
                    LIMIT 1
                    """,
                    (phrase, phrase, threshold),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                return LearnedIntent(
                    id=row[0], phrase=row[1], category=row[2],
                    tool_name=row[3], tool_args=row[4] or {},
                    mqtt_topic=row[5], mqtt_payload=row[6] or {},
                    description=row[7], use_count=row[8],
                )
        try:
            return self._db.run(_query)
        except Exception:
            _log.exception("find_match_failed")
            return None

    def save(
        self,
        *,
        phrase: str,
        category: str,
        tool_name: str = "",
        tool_args: Optional[Dict[str, Any]] = None,
        mqtt_topic: str = "",
        mqtt_payload: Optional[Dict[str, Any]] = None,
        description: str = "",
    ) -> Optional[int]:
        """Save a new learned intent. Returns the row id."""
        import json
        def _insert(conn):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO learned_intents
                        (phrase, category, tool_name, tool_args,
                         mqtt_topic, mqtt_payload, description, last_used_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                    RETURNING id
                    """,
                    (
                        phrase, category, tool_name,
                        json.dumps(tool_args or {}),
                        mqtt_topic,
                        json.dumps(mqtt_payload or {}),
                        description,
                    ),
                )
                row = cur.fetchone()
                return row[0] if row else None
        try:
            row_id = self._db.run(_insert)
            _log.info("saved", phrase=phrase[:50], category=category, id=row_id)
            return row_id
        except Exception:
            _log.exception("save_failed")
            return None

    def record_use(self, intent_id: int) -> None:
        """Bump use_count and last_used_at for a learned intent."""
        def _update(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE learned_intents SET use_count = use_count + 1, last_used_at = now() WHERE id = %s",
                    (intent_id,),
                )
        try:
            self._db.run(_update)
        except Exception:
            _log.exception("record_use_failed")

    def count(self) -> int:
        def _count(conn):
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM learned_intents")
                row = cur.fetchone()
                return row[0] if row else 0
        try:
            return self._db.run(_count)
        except Exception:
            return 0
