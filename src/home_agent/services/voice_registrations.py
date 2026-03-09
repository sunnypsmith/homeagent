"""Register all system capabilities with the SystemContext.

Called once on startup to populate query and action registries.
Dynamic registrations (Caseta devices/scenes) are updated via MQTT.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from home_agent.bus.envelope import make_event
from home_agent.bus.mqtt_client import MqttClient
from home_agent.config import AppSettings
from home_agent.core.logging import get_logger
from home_agent.services.voice_system_context import SystemContext

_log = get_logger(service="voice_registrations")


def register_all(
    ctx: SystemContext,
    settings: AppSettings,
    mqttc: MqttClient,
) -> None:
    """Register all static queries and actions from config."""
    base = settings.mqtt.base_topic

    # Helper to publish MQTT events
    def _publish(topic: str, typ: str, data: Dict[str, Any]) -> None:
        evt = make_event(source="voice-intent-agent", typ=typ, data=data)
        mqttc.publish_json("%s/%s" % (base, topic), evt)

    def _publish_retain(topic: str, typ: str, data: Dict[str, Any]) -> None:
        evt = make_event(source="voice-intent-agent", typ=typ, data=data)
        mqttc.publish_json("%s/%s" % (base, topic), evt, retain=True)

    def _announce(text: str, targets: Optional[List[str]] = None) -> None:
        data: Dict[str, Any] = {"text": text}
        if targets:
            data["targets"] = targets
        _publish("announce/request", "announce.request", data)

    def _lutron(action: str, **kwargs) -> None:
        data: Dict[str, Any] = {"action": action}
        data.update(kwargs)
        _publish("lutron/command", "lutron.command", data)

    # ==================================================================
    # QUERIES
    # ==================================================================

    # --- Time ---
    async def _query_time() -> str:
        tz = ZoneInfo(settings.timezone)
        now = datetime.now(tz=tz)
        time_str = now.strftime("%I:%M %p").lstrip("0")
        day_str = now.strftime("%A, %B %d").replace(" 0", " ")
        return "It is %s on %s." % (time_str, day_str)

    ctx.register_query("time", "Current time and date", _query_time,
                        context_fn=lambda: "Time: timezone %s" % settings.timezone)

    # --- Weather current ---
    if settings.weather.provider and settings.weather.latitude:
        async def _query_weather_current() -> str:
            from home_agent.integrations.weather import create_weather_client
            wc = create_weather_client(provider=settings.weather.provider,
                latitude=settings.weather.latitude, longitude=settings.weather.longitude,
                units=settings.weather.units, timeout_seconds=settings.weather.timeout_seconds)
            c = await wc.current()
            parts = []
            if c.temperature is not None:
                parts.append("temperature is %d degrees Fahrenheit" % int(round(c.temperature)))
            if c.description:
                parts.append(c.description.lower())
            if c.wind_speed is not None:
                wu = (c.wind_unit or "").strip().lower()
                spoken_unit = "miles per hour" if wu in ("mph", "mi/h") else c.wind_unit
                parts.append("wind %d %s" % (int(round(c.wind_speed)), spoken_unit))
            return "Currently: %s." % ", ".join(parts) if parts else "Weather data unavailable."

        async def _query_weather_forecast() -> str:
            from home_agent.integrations.weather import create_weather_client
            wc = create_weather_client(provider=settings.weather.provider,
                latitude=settings.weather.latitude, longitude=settings.weather.longitude,
                units=settings.weather.units, timeout_seconds=settings.weather.timeout_seconds)
            fc = await wc.forecast_today()
            parts = []
            if fc.temp_max is not None and fc.temp_min is not None:
                parts.append("high of %d, low of %d" % (int(round(fc.temp_max)), int(round(fc.temp_min))))
            if fc.precip_probability_max is not None and fc.precip_probability_max > 0:
                parts.append("%d percent chance of precipitation" % int(round(fc.precip_probability_max)))
            if fc.wind_speed_max is not None:
                parts.append("winds up to %d miles per hour" % int(round(fc.wind_speed_max)))
            return "Today's forecast: %s." % ", ".join(parts) if parts else "Forecast unavailable."

        ctx.register_query("weather_current",
                            "Current outdoor weather at the home (temperature, conditions, wind)",
                            _query_weather_current,
                            context_fn=lambda: "Weather: use weather_current for outside temperature/conditions, weather_forecast for today's forecast")
        ctx.register_query("weather_forecast", "Today's weather forecast (high, low, precipitation, wind)", _query_weather_forecast)

    # --- UPS ---
    if settings.ups.enabled:
        async def _query_ups() -> str:
            from home_agent.integrations.ups_snmp import UpsSnmpClient
            client = UpsSnmpClient(
                host=settings.ups.host, port=settings.ups.port,
                community=settings.ups.community, version=settings.ups.version,
                timeout_seconds=settings.ups.timeout_seconds, retries=settings.ups.retries,
            )
            reading = await client.get_input_metrics(
                voltage_oid=settings.ups.input_voltage_oid,
                frequency_oid=settings.ups.input_frequency_oid,
            )
            parts = []
            v = reading.voltage
            f = reading.frequency
            if v is not None:
                v = v * settings.ups.input_voltage_scale
                parts.append("input voltage is %.1f volts" % v)
            if f is not None:
                f = f * settings.ups.input_frequency_scale
                parts.append("frequency is %.1f hertz" % f)
            return "%s: %s." % (settings.ups.name, ", ".join(parts)) if parts else "UPS data unavailable."

        ctx.register_query("ups", "UPS input voltage and frequency", _query_ups,
                            context_fn=lambda: "UPS: %s at %s (voltage thresholds %s-%s V)" % (
                                settings.ups.name, settings.ups.host,
                                settings.ups.input_voltage_low, settings.ups.input_voltage_high))

    # --- Temp Stick ---
    if settings.tempstick.enabled:
        async def _query_tempstick() -> str:
            from home_agent.integrations.tempstick import TempStickClient
            ts_client = TempStickClient(api_key=settings.tempstick.api_key,
                                         timeout_seconds=settings.tempstick.timeout_seconds)
            sensor = await ts_client.get_sensor(sensor_id=str(settings.tempstick.sensor_id))
            if not sensor:
                return "Temp stick data unavailable."
            parts = []
            if sensor.last_temp_c is not None:
                temp_f = sensor.last_temp_c * 9.0 / 5.0 + 32.0
                parts.append("temperature is %d degrees" % int(round(temp_f)))
            if sensor.last_humidity is not None:
                parts.append("humidity is %d percent" % int(round(sensor.last_humidity)))
            return "%s sensor: %s." % (settings.tempstick.sensor_name, ", ".join(parts)) if parts else "Temp stick data unavailable."

        ctx.register_query("tempstick", "Temperature and humidity sensor", _query_tempstick,
                            context_fn=lambda: "Temp Stick: %s sensor (thresholds %s-%sF, %s-%s%% humidity)" % (
                                settings.tempstick.sensor_name,
                                settings.tempstick.temp_low_f, settings.tempstick.temp_high_f,
                                settings.tempstick.humidity_low, settings.tempstick.humidity_high))

    # --- Internet ---
    if settings.internet.enabled:
        async def _query_internet() -> str:
            import asyncio
            from home_agent.integrations.internet_check import run_internet_check
            result = await asyncio.to_thread(
                run_internet_check,
                host=settings.internet.host,
                duration_seconds=settings.internet.duration_seconds,
                interval_seconds=settings.internet.interval_seconds,
                timeout_seconds=settings.internet.timeout_seconds,
            )
            return "Internet: %.1f ms average latency, %.1f percent packet loss." % (result.avg_latency_ms, result.packet_loss_pct)

        ctx.register_query("internet", "Internet latency and packet loss", _query_internet,
                            context_fn=lambda: "Internet: ping check to %s" % settings.internet.host)

    # --- Calendar ---
    if settings.gcal.enabled and settings.gcal.ics_url:
        async def _query_calendar() -> str:
            from home_agent.integrations.gcal_ics import GoogleCalendarIcsClient
            tz = ZoneInfo(settings.timezone)
            gc = GoogleCalendarIcsClient(ics_url=settings.gcal.ics_url, timeout_seconds=20.0)
            now_local = datetime.now(tz=tz)
            events = await gc.fetch_events(tz=tz, start_date=now_local.date(), days=1, max_events=10)
            if not events:
                return "No calendar events today."
            parts = []
            for e in events:
                t = e.start.strftime("%I:%M %p").lstrip("0") if hasattr(e, 'start') and e.start else ""
                parts.append("%s at %s" % (e.title, t) if t else e.title)
            return "Today's events: %s." % "; ".join(parts)

        ctx.register_query("calendar", "Today's calendar events", _query_calendar,
                            context_fn=lambda: "Calendar: ICS feed enabled")

    # --- SimpleFIN ---
    if settings.simplefin.enabled and settings.simplefin.access_url:
        async def _query_financial() -> str:
            from home_agent.integrations.simplefin import SimpleFINClient
            sf = SimpleFINClient(access_url=settings.simplefin.access_url,
                                  timeout_seconds=settings.simplefin.timeout_seconds)
            summary = await sf.financial_summary()
            return "Financial: total cash %.0f dollars, total debt %.0f dollars, net worth %.0f dollars." % (
                summary.total_cash, abs(summary.total_debt), summary.net_worth)

        ctx.register_query("financial", "Bank account balances and net worth", _query_financial,
                            context_fn=lambda: "Financial: SimpleFIN enabled")

    # --- Service health ---
    async def _query_health() -> str:
        health = ctx.get_mqtt_data("watchdog_health") or {}
        if not health:
            return "Service health data not available yet."
        ok = [k for k, v in health.items() if v.get("status") == "ok"]
        down = [k for k, v in health.items() if v.get("status") == "down"]
        errs = [k for k, v in health.items() if v.get("status") == "error"]
        parts = ["%d services running" % len(ok)]
        if down:
            parts.append("%d down: %s" % (len(down), ", ".join(down)))
        if errs:
            parts.append("%d with errors: %s" % (len(errs), ", ".join(errs)))
        return "System: %s." % ", ".join(parts)

    ctx.register_query("service_health", "Service health status", _query_health,
                        context_fn=lambda: "System health: monitored by watchdog service")

    # --- Cameras ---
    if settings.camect.enabled:
        async def _query_cameras() -> str:
            rules = settings.camect.camera_rules
            cameras = [r.split(":")[0] for r in rules.split(";") if ":" in r] if rules else []
            return "Cameras: %s (%d total). Use the Camect hub for live view." % (
                ", ".join(c.replace("_", " ") for c in cameras[:5]), len(cameras))

        ctx.register_query("cameras", "Camera list and status", _query_cameras,
                            context_fn=lambda: "Cameras: Camect hub with %d cameras" % len(
                                [r for r in (settings.camect.camera_rules or "").split(";") if r.strip()]))

    # ==================================================================
    # ACTIONS
    # ==================================================================

    # --- Announce ---
    ctx.register_action("announce", "Make a spoken announcement on Sonos speakers",
        handler=lambda: None,  # handled specially in intent agent (needs text param)
        confirmation="Done.",
        category="sonos",
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to announce"},
                "targets": {"type": "array", "items": {"type": "string"},
                            "description": "Speaker aliases. Available: %s" % ", ".join(
                                sorted(settings.sonos.speaker_alias_map.keys()))},
            },
            "required": ["text"],
        })

    # --- Mute/Unmute ---
    ctx.register_action("mute_announcements", "Temporarily mute all Sonos announcements",
        handler=lambda: None,  # handled specially
        confirmation="Done. Announcements muted.",
        category="sonos",
        parameters={
            "type": "object",
            "properties": {"minutes": {"type": "integer", "description": "Minutes to mute"}},
            "required": ["minutes"],
        })

    ctx.register_action("unmute_announcements", "Unmute Sonos announcements",
        handler=lambda: _publish_retain("announce/mute", "announce.mute", {"muted_until_unix": 0}),
        confirmation="Done. Announcements unmuted.",
        category="sonos")

    # --- Lighting (parametric — needs device_id) ---
    ctx.register_action("lights_on", "Turn on a light by device ID",
        handler=lambda: None,
        confirmation="Done.",
        category="lighting",
        parameters={
            "type": "object",
            "properties": {"device_id": {"type": "integer", "description": "Caseta device ID"}},
            "required": ["device_id"],
        })

    ctx.register_action("lights_off", "Turn off a light by device ID",
        handler=lambda: None,
        confirmation="Done.",
        category="lighting",
        parameters={
            "type": "object",
            "properties": {"device_id": {"type": "integer", "description": "Caseta device ID"}},
            "required": ["device_id"],
        })

    ctx.register_action("lights_level", "Set light brightness",
        handler=lambda: None,
        confirmation="Done.",
        category="lighting",
        parameters={
            "type": "object",
            "properties": {
                "device_id": {"type": "integer"},
                "level": {"type": "integer", "description": "Brightness 0-100"},
            },
            "required": ["device_id", "level"],
        })

    ctx.register_action("activate_scene", "Activate a Caseta lighting scene",
        handler=lambda: None,
        confirmation="Done.",
        category="lighting",
        parameters={
            "type": "object",
            "properties": {"scene_name": {"type": "string", "description": "Scene name"}},
            "required": ["scene_name"],
        })

    # --- Briefings ---
    async def _trigger_morning():
        now = datetime.now()
        variant = "weekend" if now.weekday() >= 5 else "weekday"
        _publish("time/cron/morning_briefing", "time.cron.morning_briefing", {"variant": variant, "manual": True})

    async def _trigger_exec():
        _publish("time/cron/exec_briefing", "time.cron.exec_briefing", {"manual": True})

    async def _trigger_chime():
        _publish("time/cron/hourly_chime", "time.cron.hourly_chime", {"manual": True})

    ctx.register_action("trigger_morning_briefing", "Trigger the morning briefing",
        handler=_trigger_morning, confirmation="Done. The morning briefing is on its way.", category="briefing")
    ctx.register_action("trigger_executive_briefing", "Trigger the executive briefing",
        handler=_trigger_exec, confirmation="Done. The executive briefing is on its way.", category="briefing")
    ctx.register_action("trigger_time_check", "Trigger the hourly time and temperature check",
        handler=_trigger_chime, confirmation="Done. Here comes the time check.", category="briefing")

    # --- Household commands (from UI actions config) ---
    for action in settings.ui.actions_list():
        aid = str(action.get("id") or "").strip()
        label = str(action.get("label") or "").strip()
        text = str(action.get("text") or "").strip()
        if not aid or not label or not text:
            continue

        async def _household_handler(t=text):
            _announce(t)

        ctx.register_action(
            "household_%s" % aid,
            label,
            handler=_household_handler,
            confirmation="Done. I've sent the %s announcement." % label.lower(),
            category="household",
        )

    _log.info("registrations_complete",
              queries=len(ctx.query_names),
              actions=len(ctx.action_names))


def update_caseta_devices(ctx: SystemContext, devices: List[Dict[str, Any]]) -> None:
    """Re-register lighting actions from discovered Caseta devices."""
    device_lines = []
    for d in devices:
        did = d.get("device_id", "")
        name = d.get("name", "")
        dtype = d.get("type", "")
        if name and did:
            device_lines.append("%s (id=%s, type=%s)" % (name, did, dtype))

    if device_lines:
        ctx.update_dynamic_context("caseta_devices",
            "CASETA DEVICES:\n" + "\n".join("- " + l for l in device_lines))
    _log.info("caseta_devices_updated", count=len(devices))


def update_caseta_scenes(ctx: SystemContext, scenes: List[Dict[str, Any]]) -> None:
    """Update scene names in the activate_scene tool description."""
    scene_names = [s.get("name", "") for s in scenes if s.get("name")]
    if scene_names:
        ctx.update_dynamic_context("caseta_scenes",
            "CASETA SCENES: %s" % ", ".join(scene_names))
        # Update the activate_scene action description
        reg = ctx.find_action("activate_scene")
        if reg and reg.parameters:
            reg.parameters["properties"]["scene_name"]["description"] = \
                "Scene name. Available: %s" % ", ".join(scene_names)
    _log.info("caseta_scenes_updated", count=len(scenes))


def update_watchdog_health(ctx: SystemContext, health: Dict[str, Any]) -> None:
    """Store latest watchdog health data for query_system."""
    ctx.store_mqtt_data("watchdog_health", health)
