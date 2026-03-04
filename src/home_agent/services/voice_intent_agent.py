"""Voice intent agent — receives transcribed voice commands and executes them.

Subscribes to voice.command from the voice service, uses LLM with tool calling
to classify commands vs questions, dispatches actions via MQTT, and speaks
responses through the appropriate Sonos speaker.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from home_agent.bus.envelope import make_event
from home_agent.bus.error_reporter import ErrorReporter
from home_agent.bus.mqtt_client import MqttClient
from home_agent.config import AppSettings
from home_agent.core.logging import configure_logging, get_logger
from home_agent.integrations.llm import LLMClient, LLMToolCall, LLMTextResponse
from home_agent.integrations.llm_router import LLMRouter

SYSTEM_PROMPT = """You are Higgins, a helpful home assistant. You manage a smart home.

When the user gives a COMMAND (e.g., "turn off the lights", "mute announcements", "call the kids to dinner"), use the appropriate tool to execute it.

When the user asks a QUESTION (e.g., "what time is it?", "what's the weather?"), answer conversationally in 1-3 short sentences. Format your answer for spoken audio: spell out numbers, avoid abbreviations, no URLs, no markdown.

After executing a command, do NOT respond with text. The system will generate a spoken confirmation automatically.

The user message is prefixed with [Room: name] indicating which room they are in. Use this for context — e.g., "turn off the lights" from the office means office lights.

Format ALL output for spoken text-to-speech audio:
- Spell out ALL numbers: '3' becomes 'three', '42' becomes 'forty two', '2026' becomes 'twenty twenty six'.
- Spell out times: '2:45 PM' becomes 'two forty five P M'.
- Spell out dates: 'March 3rd' not 'March 3'.
- Spell out currency: '$1,500' becomes 'fifteen hundred dollars', '$42.50' becomes 'forty two dollars and fifty cents'.
- Spell out percentages: '22%' becomes 'twenty two percent'.
- Spell out abbreviations: 'Dr.' becomes 'Doctor', 'St.' becomes 'Street', 'Ave' becomes 'Avenue'.
- No URLs, no markdown, no bullet points, no special characters.
- Use short, natural sentences suitable for listening.

Be concise. You are speaking out loud to a person in their home."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "announce",
            "description": "Make a spoken announcement on Sonos speakers. Use this when the user wants to broadcast a message to the household.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The text to announce"},
                    "targets": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional speaker aliases to target (e.g. ['kitchen_dining', 'office']). Omit for all speakers.",
                    },
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mute_announcements",
            "description": "Temporarily mute all Sonos announcements for a number of minutes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "minutes": {"type": "integer", "description": "How many minutes to mute for"},
                },
                "required": ["minutes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unmute_announcements",
            "description": "Unmute Sonos announcements (cancel any active mute).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lights_on",
            "description": "Turn on a light by device ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "integer", "description": "Caseta device ID"},
                },
                "required": ["device_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lights_off",
            "description": "Turn off a light by device ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "integer", "description": "Caseta device ID"},
                },
                "required": ["device_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lights_level",
            "description": "Set a light to a specific brightness level.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "integer", "description": "Caseta device ID"},
                    "level": {"type": "integer", "description": "Brightness 0-100"},
                },
                "required": ["device_id", "level"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "activate_scene",
            "description": "Activate a Caseta lighting scene. Available scenes: Bedtime, Daytime, Sleep, Nighttime.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scene_name": {"type": "string", "description": "Scene name"},
                },
                "required": ["scene_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "trigger_briefing",
            "description": "Trigger a briefing: 'morning' for the morning briefing, 'executive' for the executive briefing, 'chime' for the hourly time/weather chime.",
            "parameters": {
                "type": "object",
                "properties": {
                    "briefing_type": {
                        "type": "string",
                        "enum": ["morning", "executive", "chime"],
                        "description": "Type of briefing to trigger",
                    },
                },
                "required": ["briefing_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_time_and_weather",
            "description": "Get the current time and outdoor temperature. Use this when the user asks what time it is, what the temperature is, or asks about current weather conditions.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_forecast",
            "description": "Get the weather forecast for today or tomorrow. Use this when the user asks about the forecast, tomorrow's weather, or upcoming weather conditions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "day": {
                        "type": "string",
                        "enum": ["today", "tomorrow"],
                        "description": "Which day to get the forecast for",
                    },
                },
                "required": ["day"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "household_command",
            "description": "Common household announcements: 'dinner' (call to dinner), 'bedtime' (kids bedtime), 'trash' (take out trash), 'dogs_out' (let dogs out), 'dogs_in' (let dogs in), 'answer_door' (answer the door), 'kids_upstairs' (kids come upstairs), 'kids_kitchen' (kids come to kitchen).",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "enum": ["dinner", "bedtime", "trash", "dogs_out", "dogs_in", "answer_door", "kids_upstairs", "kids_kitchen"],
                    },
                },
                "required": ["command"],
            },
        },
    },
]

_HOUSEHOLD_TEXTS = {
    "dinner": "Your attention please. Dinner is ready. Please come to the dining room now.",
    "bedtime": "Your attention please. Bedtime is now. Please start getting ready now.",
    "trash": "Your attention please. Please take out the trash now.",
    "dogs_out": "Your attention please. Please let the dogs out now.",
    "dogs_in": "Your attention please. Please let the dogs in now.",
    "answer_door": "Your attention please. Please answer the door now.",
    "kids_upstairs": "Your attention please. Kids, please come upstairs now.",
    "kids_kitchen": "Your attention please. Kids, please come to the kitchen now.",
}

_HOUSEHOLD_CONFIRMATIONS = {
    "dinner": "Done. I've called everyone to dinner.",
    "bedtime": "Done. I've announced bedtime.",
    "trash": "Done. I've asked someone to take out the trash.",
    "dogs_out": "Done. I've asked someone to let the dogs out.",
    "dogs_in": "Done. I've asked someone to let the dogs in.",
    "answer_door": "Done. I've asked someone to answer the door.",
    "kids_upstairs": "Done. I've called the kids upstairs.",
    "kids_kitchen": "Done. I've called the kids to the kitchen.",
}


async def run_voice_intent_agent() -> None:
    settings = AppSettings()
    configure_logging(settings.log_level)
    log = get_logger(service="voice_intent_agent")

    mqttc = MqttClient(
        host=settings.mqtt.host,
        port=settings.mqtt.port,
        username=settings.mqtt.username,
        password=settings.mqtt.password,
        client_id="homeagent-voice-intent-agent",
    )
    await mqttc.connect()
    reporter = ErrorReporter(mqttc=mqttc, service="voice-intent-agent", base_topic=settings.mqtt.base_topic)
    reporter.start_heartbeat(interval_seconds=30.0)
    log.info("mqtt_connected", host=settings.mqtt.host, port=settings.mqtt.port)

    base = settings.mqtt.base_topic
    mqttc.subscribe("%s/voice/command" % base)
    log.info("subscribed", topic="%s/voice/command" % base)

    providers = [
        ("primary", LLMClient(
            base_url=settings.llm.base_url,
            api_key=settings.llm.api_key,
            model=settings.llm.model,
            timeout_seconds=settings.llm.timeout_seconds,
        )),
    ]
    if settings.llm_fallback.enabled:
        providers.append(
            ("fallback", LLMClient(
                base_url=settings.llm_fallback.base_url,
                api_key=settings.llm_fallback.api_key,
                model=settings.llm_fallback.model,
                timeout_seconds=settings.llm_fallback.timeout_seconds,
            ))
        )
    llm = LLMRouter(providers)
    log.info("llm_providers", count=len(providers), names=[p[0] for p in providers])

    room_speakers = settings.voice_room_speakers_parsed
    log.info("room_speaker_map", map=room_speakers)

    def _speakers_for_room(room_id: str) -> Optional[List[str]]:
        return room_speakers.get(room_id)

    def _publish_announce(text: str, targets: Optional[List[str]] = None) -> None:
        data: Dict[str, Any] = {"text": text}
        if targets:
            data["targets"] = targets
        evt = make_event(source="voice-intent-agent", typ="announce.request", data=data)
        mqttc.publish_json("%s/announce/request" % base, evt)

    def _publish_mute(minutes: int) -> None:
        muted_until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        evt = make_event(
            source="voice-intent-agent",
            typ="announce.mute",
            data={"muted_until_unix": int(muted_until.timestamp()), "duration_minutes": minutes},
        )
        mqttc.publish_json("%s/announce/mute" % base, evt, retain=True)

    def _publish_unmute() -> None:
        evt = make_event(
            source="voice-intent-agent",
            typ="announce.mute",
            data={"muted_until_unix": 0},
        )
        mqttc.publish_json("%s/announce/mute" % base, evt, retain=True)

    def _publish_lutron(action: str, **kwargs) -> None:
        data: Dict[str, Any] = {"action": action}
        data.update(kwargs)
        evt = make_event(source="voice-intent-agent", typ="lutron.command", data=data)
        mqttc.publish_json("%s/lutron/command" % base, evt)

    def _publish_trigger(topic_suffix: str, event_type: str, **data_kwargs) -> None:
        evt = make_event(source="voice-intent-agent", typ=event_type, data={"manual": True, **data_kwargs})
        mqttc.publish_json("%s/%s" % (base, topic_suffix), evt)

    async def _execute_tool(tc: LLMToolCall) -> str:
        """Execute a tool call and return a confirmation sentence."""
        name = tc.name
        args = tc.arguments
        log.info("tool_execute", tool=name, args=args)

        if name == "announce":
            _publish_announce(args["text"], args.get("targets"))
            return "Done. I've made the announcement."

        elif name == "mute_announcements":
            minutes = int(args.get("minutes", 60))
            _publish_mute(minutes)
            return "Done. Announcements are muted for %d minutes." % minutes

        elif name == "unmute_announcements":
            _publish_unmute()
            return "Done. Announcements are unmuted."

        elif name == "lights_on":
            _publish_lutron("on", device_id=int(args["device_id"]))
            return "Done. I've turned on the light."

        elif name == "lights_off":
            _publish_lutron("off", device_id=int(args["device_id"]))
            return "Done. I've turned off the light."

        elif name == "lights_level":
            level = int(args["level"])
            _publish_lutron("level", device_id=int(args["device_id"]), level=level)
            return "Done. I've set the light to %d percent." % level

        elif name == "activate_scene":
            scene = str(args["scene_name"])
            _publish_lutron("scene", scene_name=scene)
            return "Done. I've activated the %s scene." % scene

        elif name == "trigger_briefing":
            bt = str(args.get("briefing_type", ""))
            if bt == "morning":
                now_local = datetime.now()
                variant = "weekend" if now_local.weekday() >= 5 else "weekday"
                _publish_trigger("time/cron/morning_briefing", "time.cron.morning_briefing", variant=variant)
                return "Done. The morning briefing is on its way."
            elif bt == "executive":
                _publish_trigger("time/cron/exec_briefing", "time.cron.exec_briefing")
                return "Done. The executive briefing is on its way."
            elif bt == "chime":
                _publish_trigger("time/cron/hourly_chime", "time.cron.hourly_chime")
                return "Done. Here comes the time check."
            return "I didn't recognize that briefing type."

        elif name == "get_time_and_weather":
            from datetime import datetime
            from zoneinfo import ZoneInfo
            try:
                from home_agent.integrations.weather import create_weather_client
                tz = ZoneInfo(settings.timezone)
                now_local = datetime.now(tz=tz)
                time_str = now_local.strftime("%I:%M %p").lstrip("0")
                day_str = now_local.strftime("%A, %B %d").replace(" 0", " ")

                weather_client = create_weather_client(
                    provider=settings.weather.provider,
                    latitude=settings.weather.latitude,
                    longitude=settings.weather.longitude,
                    units=settings.weather.units,
                    timeout_seconds=settings.weather.timeout_seconds,
                )
                current = await weather_client.current()
                temp = int(round(current.temperature)) if current.temperature is not None else None
                if temp is not None:
                    return "It is %s on %s, and the current temperature is %d degrees." % (time_str, day_str, temp)
                else:
                    return "It is %s on %s." % (time_str, day_str)
            except Exception:
                from datetime import datetime
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(settings.timezone)
                now_local = datetime.now(tz=tz)
                time_str = now_local.strftime("%I:%M %p").lstrip("0")
                return "It is %s." % time_str

        elif name == "get_forecast":
            from home_agent.integrations.weather import create_weather_client
            from datetime import datetime
            from zoneinfo import ZoneInfo
            try:
                day = str(args.get("day", "today"))
                tz = ZoneInfo(settings.timezone)
                weather_client = create_weather_client(
                    provider=settings.weather.provider,
                    latitude=settings.weather.latitude,
                    longitude=settings.weather.longitude,
                    units=settings.weather.units,
                    timeout_seconds=settings.weather.timeout_seconds,
                )
                fc = await weather_client.forecast_today()
                parts = []
                if day == "tomorrow":
                    # NWS forecast includes tonight/tomorrow in the periods
                    # For now, use today's forecast as approximation
                    # TODO: add multi-day forecast support to weather client
                    now_local = datetime.now(tz=tz)
                    import calendar
                    tomorrow_name = calendar.day_name[(now_local.weekday() + 1) % 7]
                    prefix = "The forecast for tomorrow, %s" % tomorrow_name
                else:
                    now_local = datetime.now(tz=tz)
                    prefix = "Today's forecast"
                if fc.temp_max is not None and fc.temp_min is not None:
                    parts.append("a high of %d and a low of %d degrees" % (int(round(fc.temp_max)), int(round(fc.temp_min))))
                if fc.precip_probability_max is not None and fc.precip_probability_max > 0:
                    parts.append("%d percent chance of precipitation" % int(round(fc.precip_probability_max)))
                if fc.wind_speed_max is not None:
                    parts.append("winds up to %d miles per hour" % int(round(fc.wind_speed_max)))
                current = await weather_client.current()
                if current.description:
                    parts.append("currently %s" % current.description.lower())
                if parts:
                    return "%s: %s." % (prefix, ", ".join(parts))
                return "%s is not available right now." % prefix
            except Exception:
                return "I was unable to get the forecast right now."

        elif name == "household_command":
            cmd = str(args.get("command", ""))
            text = _HOUSEHOLD_TEXTS.get(cmd)
            if text:
                _publish_announce(text)
                return _HOUSEHOLD_CONFIRMATIONS.get(cmd, "Done.")
            return "I didn't recognize that household command."

        return "I executed the command."

    # Status loop
    async def _status_loop() -> None:
        while True:
            await asyncio.sleep(60.0)
            log.info("status", mqtt_connected=mqttc.is_connected, room_speakers=len(room_speakers))

    status_task = asyncio.create_task(_status_loop())

    try:
        while True:
            msg = await mqttc.next_message()
            try:
                payload = msg.json()
            except Exception:
                continue

            typ = payload.get("type", "")
            if typ != "voice.command":
                continue

            data = payload.get("data") or {}
            text = str(data.get("text") or "").strip()
            room_id = str(data.get("room_id") or "")
            room_name = str(data.get("room_name") or room_id)

            if not text:
                continue

            speakers = _speakers_for_room(room_id)
            log.info("voice_command", room=room_name, room_id=room_id, text=text, speakers=speakers)

            try:
                messages = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": "[Room: %s] %s" % (room_name, text)},
                ]
                result = await llm.chat_with_tools(
                    messages=messages,
                    tools=TOOLS,
                    max_tokens=512,
                    temperature=0.3,
                )

                if isinstance(result, LLMToolCall):
                    confirmation = await _execute_tool(result)
                    log.info("tool_confirmed", tool=result.name, confirmation=confirmation)
                    _publish_announce(confirmation, speakers)

                elif isinstance(result, LLMTextResponse):
                    answer = result.text
                    if answer:
                        log.info("question_answered", room=room_name, answer=answer[:100])
                        _publish_announce(answer, speakers)
                    else:
                        log.warning("empty_llm_response", room=room_name)

            except Exception as e:
                log.exception("intent_processing_failed", room=room_name, text=text)
                reporter.report_error("intent_processing_failed", e)
                _publish_announce(
                    "I'm sorry, I had trouble processing that request.",
                    speakers,
                )

    finally:
        status_task.cancel()
        await mqttc.close()


def main() -> int:
    asyncio.run(run_voice_intent_agent())
    return 0
