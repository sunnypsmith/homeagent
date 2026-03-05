"""Tests for the LearnedActionsStore."""
from __future__ import annotations

import json

import pytest

from home_agent.integrations.learned_actions import LearnedActionsStore, LearnedAction


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "learned.json"
    return LearnedActionsStore(path=str(path))


def test_load_empty_returns_empty(store: LearnedActionsStore) -> None:
    assert store.all() == []


def test_save_action_creates_entry(store: LearnedActionsStore) -> None:
    action = store.save_action(
        phrase="turn on porch",
        room_id="offi",
        mqtt_topic="homeagent/lutron/command",
        mqtt_payload={"action": "on", "device_id": 10},
        description="Turn on the porch light",
    )
    assert isinstance(action, LearnedAction)
    assert action.phrase == "turn on porch"
    assert action.use_count == 0
    assert len(store.all()) == 1


def test_save_action_persists_to_file(tmp_path) -> None:
    path = tmp_path / "learned.json"
    store = LearnedActionsStore(path=str(path))
    store.save_action(
        phrase="test phrase",
        room_id="r1",
        mqtt_topic="topic",
        mqtt_payload={"a": 1},
        description="desc",
    )
    assert path.exists()
    data = json.loads(path.read_text())
    assert len(data) == 1
    assert data[0]["phrase"] == "test phrase"


def test_find_candidates_returns_all(store: LearnedActionsStore) -> None:
    store.save_action("a", "r1", "t", {}, "d")
    store.save_action("b", "r2", "t", {}, "d")
    assert len(store.find_candidates()) == 2


def test_find_candidates_filters_by_room(store: LearnedActionsStore) -> None:
    store.save_action("a", "r1", "t", {}, "d")
    store.save_action("b", "r2", "t", {}, "d")
    store.save_action("c", "", "t", {}, "d")  # empty room_id matches all

    result = store.find_candidates(room_id="r1")
    phrases = [a.phrase for a in result]
    assert "a" in phrases
    assert "c" in phrases  # empty room_id is global
    assert "b" not in phrases


def test_record_use_increments(store: LearnedActionsStore) -> None:
    action = store.save_action("x", "r1", "t", {}, "d")
    assert action.use_count == 0
    store.record_use(action)
    assert action.use_count == 1
    assert action.last_used_at != ""


def test_remove_deletes_action(store: LearnedActionsStore) -> None:
    store.save_action("keep", "r1", "t", {}, "d")
    store.save_action("remove_me", "r1", "t", {}, "d")
    assert len(store.all()) == 2

    removed = store.remove("remove_me")
    assert removed is True
    assert len(store.all()) == 1
    assert store.all()[0].phrase == "keep"


def test_remove_nonexistent_returns_false(store: LearnedActionsStore) -> None:
    assert store.remove("ghost") is False


def test_file_round_trip(tmp_path) -> None:
    path = tmp_path / "learned.json"
    store1 = LearnedActionsStore(path=str(path))
    store1.save_action("my phrase", "rm1", "topic/x", {"key": "val"}, "my desc")

    store2 = LearnedActionsStore(path=str(path))
    actions = store2.all()
    assert len(actions) == 1
    a = actions[0]
    assert a.phrase == "my phrase"
    assert a.room_id == "rm1"
    assert a.mqtt_topic == "topic/x"
    assert a.mqtt_payload == {"key": "val"}
    assert a.description == "my desc"
