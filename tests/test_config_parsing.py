"""Tests for config parsing helpers."""
from __future__ import annotations

import os

import pytest

from home_agent.config import AppSettings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Prevent .env file from polluting tests."""
    monkeypatch.delenv("VOICE_ROOMS", raising=False)
    monkeypatch.delenv("VOICE_ROOM_SPEAKERS", raising=False)


def test_voice_rooms_parsed_basic(monkeypatch) -> None:
    monkeypatch.setenv("VOICE_ROOMS", "office=offi,kitchen=ktch")
    s = AppSettings()
    result = s.voice_rooms_parsed
    assert result == {"office": "offi", "kitchen": "ktch"}


def test_voice_rooms_parsed_empty(monkeypatch) -> None:
    monkeypatch.setenv("VOICE_ROOMS", "")
    s = AppSettings()
    assert s.voice_rooms_parsed == {}


def test_voice_room_speakers_parsed_basic(monkeypatch) -> None:
    monkeypatch.setenv("VOICE_ROOM_SPEAKERS", "offi:office,ktch:kitchen_dining")
    s = AppSettings()
    result = s.voice_room_speakers_parsed
    assert result == {"offi": ["office"], "ktch": ["kitchen_dining"]}


def test_voice_room_speakers_parsed_multiple_speakers(monkeypatch) -> None:
    monkeypatch.setenv("VOICE_ROOM_SPEAKERS", "offi:office+hallway,ktch:kitchen_dining")
    s = AppSettings()
    result = s.voice_room_speakers_parsed
    assert result["offi"] == ["office", "hallway"]
    assert result["ktch"] == ["kitchen_dining"]


def test_voice_room_speakers_parsed_empty(monkeypatch) -> None:
    monkeypatch.setenv("VOICE_ROOM_SPEAKERS", "")
    s = AppSettings()
    assert s.voice_room_speakers_parsed == {}


def test_voice_rooms_parsed_strips_room_id_to_4_chars(monkeypatch) -> None:
    monkeypatch.setenv("VOICE_ROOMS", "longname=abcdef")
    s = AppSettings()
    result = s.voice_rooms_parsed
    assert result == {"longname": "abcd"}
