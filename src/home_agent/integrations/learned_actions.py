"""Learned actions store.

Saves confirmed custom_action results to a JSON file so they can be
recalled on future similar requests without re-reasoning.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from home_agent.core.logging import get_logger

_log = get_logger(service="learned_actions")

_DEFAULT_PATH = "data/learned_actions.json"


@dataclass
class LearnedAction:
    phrase: str
    room_id: str
    mqtt_topic: str
    mqtt_payload: Dict[str, Any]
    description: str
    confirmed_at: str
    use_count: int = 0
    last_used_at: str = ""


class LearnedActionsStore:
    def __init__(self, path: Optional[str] = None) -> None:
        self._path = Path(path or _DEFAULT_PATH)
        self._actions: List[LearnedAction] = []
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._actions = []
            return
        try:
            data = json.loads(self._path.read_text())
            self._actions = [LearnedAction(**item) for item in data]
            _log.info("loaded", count=len(self._actions), path=str(self._path))
        except Exception:
            _log.exception("load_failed", path=str(self._path))
            self._actions = []

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(
                [asdict(a) for a in self._actions], indent=2, ensure_ascii=False,
            ))
        except Exception:
            _log.exception("save_failed")

    def all(self) -> List[LearnedAction]:
        return list(self._actions)

    def save_action(self, phrase: str, room_id: str, mqtt_topic: str,
                    mqtt_payload: Dict[str, Any], description: str) -> LearnedAction:
        now = datetime.now(timezone.utc).isoformat()
        action = LearnedAction(
            phrase=phrase, room_id=room_id,
            mqtt_topic=mqtt_topic, mqtt_payload=mqtt_payload,
            description=description, confirmed_at=now,
            use_count=0, last_used_at=now,
        )
        self._actions.append(action)
        self._save()
        _log.info("saved", phrase=phrase[:50], topic=mqtt_topic)
        return action

    def record_use(self, action: LearnedAction) -> None:
        action.use_count += 1
        action.last_used_at = datetime.now(timezone.utc).isoformat()
        self._save()

    def remove(self, phrase: str) -> bool:
        before = len(self._actions)
        self._actions = [a for a in self._actions if a.phrase != phrase]
        if len(self._actions) < before:
            self._save()
            return True
        return False

    def find_candidates(self, room_id: Optional[str] = None) -> List[LearnedAction]:
        """Get all learned actions, optionally filtered by room."""
        if room_id:
            return [a for a in self._actions if a.room_id == room_id or a.room_id == ""]
        return list(self._actions)
