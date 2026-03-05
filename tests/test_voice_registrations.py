"""Tests for voice registrations."""
from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from home_agent.config import AppSettings
from home_agent.services.voice_system_context import SystemContext
from home_agent.services.voice_registrations import (
    register_all,
    update_caseta_devices,
    update_caseta_scenes,
    update_watchdog_health,
)


class FakeMqtt:
    """Minimal stand-in for MqttClient."""

    def __init__(self):
        self.published = []

    def publish_json(self, topic: str, payload: Any, *, retain: bool = False) -> None:
        self.published.append((topic, payload, retain))


@pytest.fixture
def reg_ctx(monkeypatch):
    """Register all capabilities and return (ctx, settings, mqtt)."""
    monkeypatch.setenv("VOICE_ROOMS", "office=offi,kitchen=ktch")
    monkeypatch.setenv("VOICE_ROOM_SPEAKERS", "offi:office,ktch:kitchen_dining")
    monkeypatch.setenv("WEATHER_PROVIDER", "open_meteo")
    monkeypatch.setenv("WEATHER_LAT", "40.0")
    monkeypatch.setenv("WEATHER_LON", "-74.0")
    monkeypatch.setenv("UI_ENABLED", "true")
    # Default UI actions will be used (dinner, kids_up)
    settings = AppSettings()
    ctx = SystemContext()
    mqttc = FakeMqtt()
    register_all(ctx, settings, mqttc)
    return ctx, settings, mqttc


def test_register_all_creates_queries(reg_ctx) -> None:
    ctx, _, _ = reg_ctx
    assert len(ctx.query_names) >= 3  # time, weather_current, weather_forecast, service_health


def test_register_all_creates_actions(reg_ctx) -> None:
    ctx, _, _ = reg_ctx
    assert len(ctx.action_names) >= 5  # announce, mute, unmute, lights, briefings, household


def test_all_registered_queries_have_callable(reg_ctx) -> None:
    ctx, _, _ = reg_ctx
    for name in ctx.query_names:
        reg = ctx._queries[name]
        assert callable(reg.query_fn), f"query_fn for {name} is not callable"


def test_all_registered_actions_have_handler(reg_ctx) -> None:
    ctx, _, _ = reg_ctx
    for name in ctx.action_names:
        reg = ctx._actions[name]
        assert callable(reg.handler), f"handler for {name} is not callable"


def test_household_commands_registered(reg_ctx) -> None:
    ctx, _, _ = reg_ctx
    household = [n for n in ctx.action_names if n.startswith("household_")]
    assert len(household) >= 2  # dinner + kids_up from default


def test_briefing_actions_registered(reg_ctx) -> None:
    ctx, _, _ = reg_ctx
    assert "trigger_morning_briefing" in ctx.action_names
    assert "trigger_executive_briefing" in ctx.action_names
    assert "trigger_time_check" in ctx.action_names


def test_update_caseta_scenes_updates_context(reg_ctx) -> None:
    ctx, _, _ = reg_ctx
    scenes = [{"name": "Evening"}, {"name": "Movie Time"}]
    update_caseta_scenes(ctx, scenes)
    prompt = ctx.build_prompt_context()
    assert "Evening" in prompt
    assert "Movie Time" in prompt


def test_update_caseta_devices_updates_context(reg_ctx) -> None:
    ctx, _, _ = reg_ctx
    devices = [{"device_id": "10", "name": "Porch Light", "type": "dimmer"}]
    update_caseta_devices(ctx, devices)
    prompt = ctx.build_prompt_context()
    assert "Porch Light" in prompt


def test_update_watchdog_health_stores_data(reg_ctx) -> None:
    ctx, _, _ = reg_ctx
    health = {"svc1": {"status": "ok"}, "svc2": {"status": "down"}}
    update_watchdog_health(ctx, health)
    assert ctx.get_mqtt_data("watchdog_health") == health
