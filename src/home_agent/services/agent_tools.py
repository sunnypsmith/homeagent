"""Parameterized tools for the Claude agent loop.

Each tool has a definition (OpenAI format for Claude conversion) and an
async executor function. Claude calls tools with parameters it determines
from the user's request; executors fetch data or perform actions and return
text results.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from home_agent.bus.envelope import make_event
from home_agent.bus.mqtt_client import MqttClient
from home_agent.config import AppSettings
from home_agent.core.logging import get_logger

_log = get_logger(service="agent_tools")


def _tool(name: str, description: str, properties: Dict[str, Any],
          required: Optional[List[str]] = None) -> Dict[str, Any]:
    params: Dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        params["required"] = required
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": params},
    }


def build_tools(settings: AppSettings) -> List[Dict[str, Any]]:
    """Build the full tool list for the Claude agent."""
    tools = []

    if settings.weather.provider and settings.weather.latitude:
        tools.append(_tool(
            "get_weather",
            "Get weather information. Use timeframe='now' for current conditions, "
            "'today' for today's forecast, 'tomorrow' for tomorrow, or a day name like 'Wednesday'.",
            {"timeframe": {"type": "string", "description": "now, today, tomorrow, or a day name"}},
            required=["timeframe"],
        ))

    tools.append(_tool(
        "get_time", "Get the current time and date.", {}, required=[],
    ))

    if settings.ups.enabled:
        tools.append(_tool(
            "get_system_status",
            "Get status of a home system: ups, internet, sensors, cameras, services, or all.",
            {"system": {"type": "string", "description": "Which system: ups, internet, sensors, cameras, services, all"}},
            required=["system"],
        ))

    if settings.gcal.enabled and settings.gcal.ics_url:
        tools.append(_tool(
            "get_calendar",
            "Get calendar events. Timeframe: today, tomorrow, this week, or a specific date.",
            {"timeframe": {"type": "string", "description": "today, tomorrow, this week, or a date"}},
            required=["timeframe"],
        ))

    if settings.simplefin.enabled and settings.simplefin.access_url:
        tools.append(_tool(
            "get_financial_summary",
            "Get bank account balances and net worth.",
            {}, required=[],
        ))

    speaker_aliases = sorted(settings.sonos.speaker_alias_map.keys())
    tools.append(_tool(
        "make_announcement",
        "Make a spoken announcement on Sonos speakers.",
        {
            "text": {"type": "string", "description": "Text to announce"},
            "rooms": {"type": "array", "items": {"type": "string"},
                      "description": "Room names: %s. Omit for all." % ", ".join(speaker_aliases)},
        },
        required=["text"],
    ))

    tools.append(_tool(
        "mute_announcements",
        "Temporarily mute all announcements.",
        {"minutes": {"type": "integer", "description": "How many minutes to mute"}},
        required=["minutes"],
    ))

    tools.append(_tool(
        "unmute_announcements",
        "Unmute announcements immediately.",
        {}, required=[],
    ))

    tools.append(_tool(
        "control_light",
        "Control a light. Action: on, off, or level (with brightness 0-100).",
        {
            "device_name": {"type": "string", "description": "Light/device name"},
            "action": {"type": "string", "description": "on, off, or level"},
            "brightness": {"type": "integer", "description": "Brightness 0-100 (only for action=level)"},
        },
        required=["device_name", "action"],
    ))

    tools.append(_tool(
        "activate_scene",
        "Activate a lighting scene by name.",
        {"scene_name": {"type": "string", "description": "Scene name"}},
        required=["scene_name"],
    ))

    tools.append(_tool(
        "trigger_briefing",
        "Trigger a briefing: morning, executive, or time_check.",
        {"type": {"type": "string", "description": "morning, executive, or time_check"}},
        required=["type"],
    ))

    tools.append(_tool(
        "household_announcement",
        "Make a household announcement: dinner, bedtime, dogs_out, dogs_in, kids_kitchen, kids_upstairs, trash, answer_door.",
        {"type": {"type": "string", "description": "dinner, bedtime, dogs_out, dogs_in, kids_kitchen, kids_upstairs, trash, answer_door"}},
        required=["type"],
    ))

    tools.append(_tool(
        "web_search",
        "Search the web for information not available from local tools. "
        "Use for questions about businesses, events, news, general knowledge, "
        "restaurants, directions, sports, or anything outside the home.",
        {"query": {"type": "string", "description": "The search query"}},
        required=["query"],
    ))

    return tools


class ToolExecutor:
    """Executes agent tool calls by dispatching to the appropriate integration."""

    def __init__(self, settings: AppSettings, mqttc: MqttClient, ctx: Any,
                 perplexity_llm: Any = None) -> None:
        self._settings = settings
        self._mqttc = mqttc
        self._ctx = ctx
        self._base = settings.mqtt.base_topic
        self._weather_client = None
        self._perplexity_llm = perplexity_llm
        self._tz = ZoneInfo(settings.timezone)

    def _publish(self, topic: str, typ: str, data: Dict[str, Any]) -> None:
        evt = make_event(source="voice-intent-agent", typ=typ, data=data)
        self._mqttc.publish_json("%s/%s" % (self._base, topic), evt)

    async def execute(self, tool_name: str, args: Dict[str, Any]) -> str:
        handler = getattr(self, "_tool_%s" % tool_name, None)
        if handler is None:
            return "Unknown tool: %s" % tool_name
        try:
            return await handler(args)
        except Exception as e:
            _log.exception("tool_execute_failed", tool=tool_name)
            return "Error executing %s: %s" % (tool_name, str(e)[:200])

    async def _get_weather_client(self):
        if self._weather_client is None:
            from home_agent.integrations.weather import create_weather_client
            self._weather_client = create_weather_client(
                provider=self._settings.weather.provider,
                latitude=self._settings.weather.latitude,
                longitude=self._settings.weather.longitude,
                units=self._settings.weather.units,
                timeout_seconds=self._settings.weather.timeout_seconds,
            )
        return self._weather_client

    async def _tool_get_weather(self, args: Dict[str, Any]) -> str:
        wc = await self._get_weather_client()
        tf = str(args.get("timeframe", "now")).strip().lower()

        if tf == "now":
            c = await wc.current()
            parts = []
            if c.temperature is not None:
                parts.append("temperature is %d degrees %s" % (
                    int(round(c.temperature)), getattr(c, "temperature_unit", "F") or "F"))
            if c.description:
                parts.append(c.description.lower())
            if c.wind_speed is not None:
                parts.append("wind %d %s" % (
                    int(round(c.wind_speed)), getattr(c, "wind_unit", "mph") or "mph"))
            humidity = getattr(c, "humidity", None)
            if humidity is not None:
                parts.append("humidity %d percent" % int(round(humidity)))
            return "Currently: %s." % ", ".join(parts) if parts else "Current weather data unavailable."

        if not hasattr(wc, "forecast_periods"):
            fc = await wc.forecast_today()
            parts = []
            if fc.temp_max is not None and fc.temp_min is not None:
                parts.append("high of %d, low of %d" % (int(round(fc.temp_max)), int(round(fc.temp_min))))
            if fc.precip_probability_max is not None and fc.precip_probability_max > 0:
                parts.append("%d percent chance of precipitation" % int(round(fc.precip_probability_max)))
            if fc.wind_speed_max is not None:
                parts.append("winds up to %d mph" % int(round(fc.wind_speed_max)))
            label = tf if tf != "today" else "Today"
            return "%s forecast: %s." % (label.capitalize(), ", ".join(parts)) if parts else "Forecast unavailable for %s." % tf

        periods = await wc.forecast_periods()
        if not periods:
            return "Forecast data unavailable."

        now = datetime.now(tz=self._tz)
        target_periods = []

        if tf == "today":
            today_str = now.strftime("%A")
            target_periods = [p for p in periods if today_str.lower() in p.get("name", "").lower()
                              or p.get("name", "").lower() in ("today", "this afternoon", "tonight")]
            if not target_periods:
                target_periods = periods[:2]
        elif tf == "tomorrow":
            tomorrow = now + timedelta(days=1)
            tomorrow_str = tomorrow.strftime("%A")
            target_periods = [p for p in periods if tomorrow_str.lower() in p.get("name", "").lower()]
            if not target_periods:
                target_periods = periods[2:4] if len(periods) > 3 else periods[-2:]
        elif tf in ("this weekend", "weekend"):
            target_periods = [p for p in periods
                              if "saturday" in p.get("name", "").lower()
                              or "sunday" in p.get("name", "").lower()]
        else:
            target_periods = [p for p in periods if tf in p.get("name", "").lower()]

        if not target_periods:
            return "No forecast data found for '%s'. Available periods: %s" % (
                tf, ", ".join(p.get("name", "") for p in periods[:6]))

        lines = []
        for p in target_periods:
            name = p.get("name", "")
            temp = p.get("temperature", "")
            unit = p.get("temperatureUnit", "F")
            short = p.get("shortForecast", "")
            wind = p.get("windSpeed", "")
            precip = p.get("probabilityOfPrecipitation", {})
            precip_val = precip.get("value") if isinstance(precip, dict) else None
            line = "%s: %s degrees %s, %s" % (name, temp, unit, short)
            if wind:
                line += ", wind %s" % wind
            if precip_val and precip_val > 0:
                line += ", %d percent chance of precipitation" % precip_val
            lines.append(line)
        return "\n".join(lines)

    async def _tool_get_time(self, args: Dict[str, Any]) -> str:
        now = datetime.now(tz=self._tz)
        time_str = now.strftime("%I:%M %p").lstrip("0")
        day_str = now.strftime("%A, %B %d").replace(" 0", " ")
        return "It is %s on %s." % (time_str, day_str)

    async def _tool_get_system_status(self, args: Dict[str, Any]) -> str:
        system = str(args.get("system", "all")).strip().lower()
        parts = []

        if system in ("ups", "all") and self._settings.ups.enabled:
            try:
                from home_agent.integrations.ups_snmp import UpsSnmpClient
                client = UpsSnmpClient(
                    host=self._settings.ups.host, port=self._settings.ups.port,
                    community=self._settings.ups.community, version=self._settings.ups.version,
                    timeout_seconds=self._settings.ups.timeout_seconds, retries=self._settings.ups.retries,
                )
                reading = await client.get_input_metrics(
                    voltage_oid=self._settings.ups.input_voltage_oid,
                    frequency_oid=self._settings.ups.input_frequency_oid,
                )
                v = reading.voltage
                if v is not None:
                    v = v * self._settings.ups.input_voltage_scale
                    parts.append("UPS input voltage: %.1f volts" % v)
            except Exception as e:
                parts.append("UPS: error reading (%s)" % str(e)[:50])

        if system in ("internet", "all") and self._settings.internet.enabled:
            try:
                import asyncio
                from home_agent.integrations.internet_check import run_internet_check
                result = await asyncio.to_thread(
                    run_internet_check,
                    host=self._settings.internet.host,
                    duration_seconds=self._settings.internet.duration_seconds,
                    interval_seconds=self._settings.internet.interval_seconds,
                    timeout_seconds=self._settings.internet.timeout_seconds,
                )
                parts.append("Internet: %.1f ms latency, %.1f%% packet loss" % (result.avg_latency_ms, result.packet_loss_pct))
            except Exception as e:
                parts.append("Internet: error (%s)" % str(e)[:50])

        if system in ("sensors", "all") and self._settings.tempstick.enabled:
            try:
                from home_agent.integrations.tempstick import TempStickClient
                ts = TempStickClient(api_key=self._settings.tempstick.api_key,
                                     timeout_seconds=self._settings.tempstick.timeout_seconds)
                sensor = await ts.get_sensor(sensor_id=str(self._settings.tempstick.sensor_id))
                if sensor and sensor.last_temp_c is not None:
                    temp_f = sensor.last_temp_c * 9.0 / 5.0 + 32.0
                    hum = sensor.last_humidity
                    parts.append("%s sensor: %d F%s" % (
                        self._settings.tempstick.sensor_name,
                        int(round(temp_f)),
                        ", %d%% humidity" % int(round(hum)) if hum is not None else ""))
            except Exception as e:
                parts.append("Sensors: error (%s)" % str(e)[:50])

        if system in ("services", "all"):
            health = self._ctx.get_mqtt_data("watchdog_health") or {}
            if health:
                ok = len([v for v in health.values() if v.get("status") == "ok"])
                down = [k for k, v in health.items() if v.get("status") == "down"]
                errs = [k for k, v in health.items() if v.get("status") == "error"]
                s = "%d services running" % ok
                if down:
                    s += ", %d down: %s" % (len(down), ", ".join(down))
                if errs:
                    s += ", %d with errors: %s" % (len(errs), ", ".join(errs))
                parts.append(s)

        return "\n".join(parts) if parts else "No status data available for '%s'." % system

    async def _tool_get_calendar(self, args: Dict[str, Any]) -> str:
        tf = str(args.get("timeframe", "today")).strip().lower()
        from home_agent.integrations.gcal_ics import GoogleCalendarIcsClient
        gc = GoogleCalendarIcsClient(ics_url=self._settings.gcal.ics_url, timeout_seconds=20.0)
        now = datetime.now(tz=self._tz)

        if tf == "tomorrow":
            start = (now + timedelta(days=1)).date()
        elif tf in ("this week", "week"):
            start = now.date()
        else:
            start = now.date()

        days = 7 if tf in ("this week", "week") else 1
        events = await gc.fetch_events(tz=self._tz, start_date=start, days=days, max_events=15)
        if not events:
            return "No calendar events found for %s." % tf
        parts = []
        for e in events:
            t = e.start.strftime("%I:%M %p").lstrip("0") if hasattr(e, "start") and e.start else ""
            d = e.start.strftime("%A") if hasattr(e, "start") and e.start else ""
            if days > 1 and d:
                parts.append("%s %s at %s" % (d, e.title, t) if t else "%s %s" % (d, e.title))
            else:
                parts.append("%s at %s" % (e.title, t) if t else e.title)
        return "Events: %s." % "; ".join(parts)

    async def _tool_get_financial_summary(self, args: Dict[str, Any]) -> str:
        from home_agent.integrations.simplefin import SimpleFINClient
        sf = SimpleFINClient(access_url=self._settings.simplefin.access_url,
                             timeout_seconds=self._settings.simplefin.timeout_seconds)
        summary = await sf.financial_summary()
        return "Total cash: $%.0f, total debt: $%.0f, net worth: $%.0f." % (
            summary.total_cash, abs(summary.total_debt), summary.net_worth)

    async def _tool_make_announcement(self, args: Dict[str, Any]) -> str:
        text = str(args.get("text", "")).strip()
        if not text:
            return "No announcement text provided."
        data: Dict[str, Any] = {"text": text}
        rooms = args.get("rooms")
        if rooms and isinstance(rooms, list):
            data["targets"] = rooms
        self._publish("announce/request", "announce.request", data)
        return "Announcement sent: %s" % text[:80]

    async def _tool_mute_announcements(self, args: Dict[str, Any]) -> str:
        minutes = int(args.get("minutes", 60))
        muted_until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        evt = make_event(source="voice-intent-agent", typ="announce.mute",
            data={"muted_until_unix": int(muted_until.timestamp()), "duration_minutes": minutes})
        self._mqttc.publish_json("%s/announce/mute" % self._base, evt, retain=True)
        return "Announcements muted for %d minutes." % minutes

    async def _tool_unmute_announcements(self, args: Dict[str, Any]) -> str:
        evt = make_event(source="voice-intent-agent", typ="announce.mute",
            data={"muted_until_unix": 0})
        self._mqttc.publish_json("%s/announce/mute" % self._base, evt, retain=True)
        return "Announcements unmuted."

    async def _tool_control_light(self, args: Dict[str, Any]) -> str:
        device_name = str(args.get("device_name", "")).strip()
        action = str(args.get("action", "")).strip().lower()
        brightness = args.get("brightness")

        caseta_devices = self._ctx.get_mqtt_data("caseta_devices") or []
        device_id = None
        for d in caseta_devices:
            if str(d.get("name", "")).lower() == device_name.lower():
                device_id = d.get("device_id")
                break
        if device_id is None:
            available = [str(d.get("name", "")) for d in caseta_devices if d.get("name")]
            return "Device '%s' not found. Available: %s" % (device_name, ", ".join(available[:10]))

        lutron_data: Dict[str, Any] = {"device_id": int(device_id)}
        if action == "level" and brightness is not None:
            lutron_data["action"] = "level"
            lutron_data["level"] = max(0, min(100, int(brightness)))
        elif action == "off":
            lutron_data["action"] = "off"
        else:
            lutron_data["action"] = "on"

        self._publish("lutron/command", "lutron.command", lutron_data)
        return "Done. %s %s." % (device_name, action)

    async def _tool_activate_scene(self, args: Dict[str, Any]) -> str:
        scene_name = str(args.get("scene_name", "")).strip()
        self._publish("lutron/command", "lutron.command", {"action": "scene", "scene_name": scene_name})
        return "Activated scene: %s." % scene_name

    async def _tool_trigger_briefing(self, args: Dict[str, Any]) -> str:
        btype = str(args.get("type", "")).strip().lower()
        type_map = {
            "morning": ("time/cron/morning_briefing", "time.cron.morning_briefing",
                        {"variant": "weekend" if datetime.now().weekday() >= 5 else "weekday", "manual": True}),
            "executive": ("time/cron/exec_briefing", "time.cron.exec_briefing", {"manual": True}),
            "time_check": ("time/cron/hourly_chime", "time.cron.hourly_chime", {"manual": True}),
        }
        if btype not in type_map:
            return "Unknown briefing type: %s. Available: morning, executive, time_check." % btype
        topic, typ, data = type_map[btype]
        self._publish(topic, typ, data)
        return "Triggered %s briefing." % btype

    async def _tool_household_announcement(self, args: Dict[str, Any]) -> str:
        htype = str(args.get("type", "")).strip().lower()
        actions = self._settings.ui.actions_list()
        for action in actions:
            aid = str(action.get("id", "")).strip().lower()
            if aid == htype or aid == "household_%s" % htype:
                text = str(action.get("text", "")).strip()
                if text:
                    self._publish("announce/request", "announce.request", {"text": text})
                    return "Done. Sent: %s" % text[:80]
        available = [str(a.get("id", "")) for a in actions if a.get("id")]
        return "Unknown household type: %s. Available: %s" % (htype, ", ".join(available))

    async def _tool_web_search(self, args: Dict[str, Any]) -> str:
        query = str(args.get("query", "")).strip()
        if not query:
            return "No search query provided."
        if not self._perplexity_llm:
            return "Web search is not available (no Perplexity API key configured)."
        import re
        try:
            answer = await self._perplexity_llm.chat(
                system="You are a helpful assistant. Answer concisely in one to three sentences. "
                       "Include specific facts, times, addresses, and details when available.",
                user=query, max_tokens=300, temperature=0.2,
            )
            if answer and len(answer.strip()) > 5:
                answer = re.sub(r"\[\d+\]|\*\*", "", answer)
                return re.sub(r"\s{2,}", " ", answer).strip()
        except Exception as e:
            _log.warning("web_search_failed", query=query[:60], error=str(e)[:100])
            return "Web search failed: %s" % str(e)[:100]
        return "No results found for: %s" % query
