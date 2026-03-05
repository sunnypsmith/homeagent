"""Tests for voice service state machine components."""
from __future__ import annotations

import time

import pytest

from home_agent.services.voice_service import RoomState, _WHISPER_HALLUCINATIONS
from home_agent.services.voice_intent_agent import PendingAction


def test_room_state_values() -> None:
    assert RoomState.LISTENING == "listening"
    assert RoomState.CAPTURING == "capturing"
    assert RoomState.PROCESSING == "processing"
    assert RoomState.DEAF == "deaf"


def test_whisper_hallucination_set_contains_expected() -> None:
    assert "thank you" in _WHISPER_HALLUCINATIONS
    assert "thanks for watching" in _WHISPER_HALLUCINATIONS
    assert "please subscribe" in _WHISPER_HALLUCINATIONS
    assert "..." in _WHISPER_HALLUCINATIONS
    assert "bye" in _WHISPER_HALLUCINATIONS
    assert isinstance(_WHISPER_HALLUCINATIONS, set)


def test_pending_action_not_expired() -> None:
    action = PendingAction(
        description="test",
        mqtt_topic="topic",
        mqtt_payload={"a": 1},
        original_text="do something",
        created_at=time.monotonic(),
        room_id="offi",
        room_name="office",
        timeout_seconds=60.0,
    )
    assert action.is_expired is False


def test_pending_action_expired() -> None:
    action = PendingAction(
        description="test",
        mqtt_topic="topic",
        mqtt_payload={"a": 1},
        original_text="do something",
        created_at=time.monotonic() - 120.0,
        room_id="offi",
        room_name="office",
        timeout_seconds=60.0,
    )
    assert action.is_expired is True


def test_pending_action_custom_timeout() -> None:
    action = PendingAction(
        description="test",
        mqtt_topic="topic",
        mqtt_payload={},
        original_text="x",
        created_at=time.monotonic() - 5.0,
        room_id="r",
        room_name="r",
        timeout_seconds=3.0,
    )
    assert action.is_expired is True
