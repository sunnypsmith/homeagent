"""Voice intent agent v2 — adaptive reasoning with confirmation.

Receives voice.command from the voice service, uses registry-based tools
with two-tier LLM (Groq for fast dispatch, Claude for reasoning),
per-room conversation history, confirmation flow for custom actions,
and learned actions store.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from home_agent.bus.envelope import make_event
from home_agent.bus.error_reporter import ErrorReporter
from home_agent.bus.mqtt_client import MqttClient
from home_agent.config import AppSettings
from home_agent.core.logging import configure_logging, get_logger
from home_agent.integrations.llm import LLMClient, LLMToolCall, LLMTextResponse
from home_agent.integrations.llm_router import LLMRouter
from home_agent.integrations.learned_actions import LearnedActionsStore
from home_agent.services.voice_system_context import SystemContext
from home_agent.services.voice_registrations import (
    register_all, update_caseta_devices, update_caseta_scenes, update_watchdog_health,
)


# ------------------------------------------------------------------
# System prompt
# ------------------------------------------------------------------

_BASE_SYSTEM_PROMPT = """You are Higgins, a helpful home assistant for the Smith family in Lynchburg, Virginia.

Use the appropriate tool when the user asks a question or gives a command. If no tool is relevant, respond conversationally.

The user message is prefixed with [Room: name] indicating which room they are in.

Format output for spoken audio: spell out all numbers, times, dates, currency, percentages, and units as words. No URLs, markdown, or special characters. Be concise."""


# ------------------------------------------------------------------
# Data structures
# ------------------------------------------------------------------

@dataclass
class PendingAction:
    description: str
    mqtt_topic: str
    mqtt_payload: Dict[str, Any]
    original_text: str
    created_at: float
    room_id: str
    room_name: str
    timeout_seconds: float = 60.0

    @property
    def is_expired(self) -> bool:
        return (time.monotonic() - self.created_at) > self.timeout_seconds


# ------------------------------------------------------------------
# Custom action tool (hard-coded intentionally — the escape hatch)
# ------------------------------------------------------------------

_CUSTOM_ACTION_TOOL = {
    "type": "function",
    "function": {
        "name": "custom_action",
        "description": (
            "For actions not covered by other tools. The system will describe your plan "
            "and ask the user for confirmation before executing. Use the MQTT architecture "
            "knowledge from the system context to construct the correct topic and payload."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "Human-readable description of the action"},
                "mqtt_topic": {"type": "string", "description": "Full MQTT topic to publish to"},
                "mqtt_payload": {"type": "object", "description": "Event data payload"},
            },
            "required": ["description", "mqtt_topic", "mqtt_payload"],
        },
    },
}


# ------------------------------------------------------------------
# Main service
# ------------------------------------------------------------------

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
    mqttc.subscribe("%s/lutron/event" % base)
    mqttc.subscribe("%s/watchdog/health" % base)
    log.info("subscribed")

    # --- LLM setup ---
    fast_providers = [
        ("primary", LLMClient(
            base_url=settings.llm.base_url, api_key=settings.llm.api_key,
            model=settings.llm.model, timeout_seconds=settings.llm.timeout_seconds,
        )),
    ]
    if settings.llm_fallback.enabled:
        fast_providers.append(
            ("fallback", LLMClient(
                base_url=settings.llm_fallback.base_url, api_key=settings.llm_fallback.api_key,
                model=settings.llm_fallback.model, timeout_seconds=settings.llm_fallback.timeout_seconds,
            ))
        )
    fast_llm = LLMRouter(fast_providers)
    log.info("fast_llm", providers=[p[0] for p in fast_providers])

    reasoning_llm = None
    if settings.voice_reasoning_api_key:
        from home_agent.integrations.llm_anthropic import AnthropicClient
        reasoning_llm = AnthropicClient(
            api_key=settings.voice_reasoning_api_key,
            model=settings.voice_reasoning_model,
            timeout_seconds=settings.voice_reasoning_timeout,
        )
        log.info("reasoning_llm", model=settings.voice_reasoning_model)

    perplexity_llm = None
    if settings.voice_perplexity_api_key:
        perplexity_llm = LLMClient(
            base_url="https://api.perplexity.ai",
            api_key=settings.voice_perplexity_api_key,
            model=settings.voice_perplexity_model,
            timeout_seconds=settings.voice_perplexity_timeout,
        )
        log.info("perplexity_llm", model=settings.voice_perplexity_model)

    # --- System context ---
    ctx = SystemContext()
    register_all(ctx, settings, mqttc)

    # --- State ---
    room_speakers = settings.voice_room_speakers_parsed
    pending_actions: Dict[str, PendingAction] = {}
    learned = LearnedActionsStore()
    log.info("ready", queries=len(ctx.query_names), actions=len(ctx.action_names),
             room_speakers=room_speakers, learned_actions=len(learned.all()))

    # --- Response topic for web chat ---
    response_topic = "%s/voice/response" % base

    # --- Helpers ---
    def _speakers_for_room(room_id: str) -> Optional[List[str]]:
        return room_speakers.get(room_id)

    def _respond(text: str, room_id: str, room_name: str, speakers: Optional[List[str]], _t0: Optional[float] = None) -> None:
        """Send a response via Sonos and/or MQTT response topic."""
        if _t0 is not None:
            log.info("intent_respond", room=room_name, text_len=len(text),
                     intent_elapsed_ms=round((time.monotonic() - _t0) * 1000))

        evt = make_event(source="voice-intent-agent", typ="voice.response",
            data={"room_id": room_id, "room_name": room_name, "text": text})
        mqttc.publish_json(response_topic, evt)

        if speakers and speakers != ["none"]:
            data: Dict[str, Any] = {"text": text, "targets": speakers, "exempt_mute": True, "exempt_quiet_hours": True}
            announce_evt = make_event(source="voice-intent-agent", typ="announce.request", data=data)
            mqttc.publish_json("%s/announce/request" % base, announce_evt)

    def _respond_ack(key: str, speakers: Optional[List[str]]) -> None:
        """Play a pre-recorded acknowledgment."""
        if speakers and speakers != ["none"]:
            data: Dict[str, Any] = {"text": key, "offline_audio_key": key, "targets": speakers, "exempt_mute": True, "exempt_quiet_hours": True}
            evt = make_event(source="voice-intent-agent", typ="announce.request", data=data)
            mqttc.publish_json("%s/announce/request" % base, evt)

    async def _execute_tool_call(tc: LLMToolCall, room_id: str, room_name: str) -> Optional[str]:
        """Execute a tool call. Returns confirmation text or None for special handling."""
        name = tc.name
        args = tc.arguments
        log.info("tool_execute", tool=name, args=args)

        # query_system
        if name == "query_system":
            query = args.get("query", "")
            return await ctx.execute_query(query)

        # announce (special — needs text param)
        if name == "announce":
            text = args.get("text", "")
            if not text:
                return "I need some text to announce."
            targets = args.get("targets")
            data: Dict[str, Any] = {"text": text}
            if targets:
                data["targets"] = targets
            evt = make_event(source="voice-intent-agent", typ="announce.request", data=data)
            mqttc.publish_json("%s/announce/request" % base, evt)
            return "Done. I've made the announcement."

        # mute (special — needs minutes param)
        if name == "mute_announcements":
            minutes = int(args.get("minutes", 60))
            muted_until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
            evt = make_event(source="voice-intent-agent", typ="announce.mute",
                data={"muted_until_unix": int(muted_until.timestamp()), "duration_minutes": minutes})
            mqttc.publish_json("%s/announce/mute" % base, evt, retain=True)
            return "Done. Announcements muted for %d minutes." % minutes

        # lighting (special — needs device_id/level params passed through)
        if name in ("lights_on", "lights_off", "lights_level", "activate_scene"):
            action_map = {"lights_on": "on", "lights_off": "off", "lights_level": "level", "activate_scene": "scene"}
            lutron_data: Dict[str, Any] = {"action": action_map[name]}
            if "device_id" in args:
                lutron_data["device_id"] = int(args["device_id"])
            if "level" in args:
                lutron_data["level"] = int(args["level"])
            if "scene_name" in args:
                lutron_data["scene_name"] = str(args["scene_name"])
            evt = make_event(source="voice-intent-agent", typ="lutron.command", data=lutron_data)
            mqttc.publish_json("%s/lutron/command" % base, evt)

            if name == "activate_scene":
                return "Done. I've activated the %s scene." % args.get("scene_name", "")
            return "Done."

        # Category compound tools (briefing_command, household_command)
        if name.endswith("_command"):
            cmd = args.get("command", "")
            reg = ctx.find_category_action(name, cmd)
            if reg:
                return await ctx.execute_action(cmd)
            return "I didn't recognize that command."

        # Direct action from registry
        reg = ctx.find_action(name)
        if reg:
            return await ctx.execute_action(name)

        return "I executed the command."

    # --- Background tasks ---

    async def _timeout_loop() -> None:
        """Cancel expired pending actions."""
        while True:
            await asyncio.sleep(10.0)
            now = time.monotonic()
            expired = [rid for rid, pa in pending_actions.items() if pa.is_expired]
            for rid in expired:
                pa = pending_actions.pop(rid)
                speakers = _speakers_for_room(rid)
                _respond_ack("voice_cancelled", speakers)
                log.info("pending_expired", room=pa.room_name)

    async def _status_loop() -> None:
        while True:
            await asyncio.sleep(60.0)
            log.info("status", mqtt_connected=mqttc.is_connected,
                     pending=len(pending_actions),
                     learned=len(learned.all()))

    timeout_task = asyncio.create_task(_timeout_loop())
    status_task = asyncio.create_task(_status_loop())

    # --- Main loop ---
    try:
        while True:
            msg = await mqttc.next_message()
            try:
                payload = msg.json()
            except Exception as e:
                log.warning("mqtt_payload_decode_failed", topic=msg.topic, error=str(e)[:100])
                continue

            typ = payload.get("type", "")
            data = payload.get("data") or {}

            # --- MQTT event routing ---

            # Caseta device discovery
            if typ == "lutron.devices":
                devices = data.get("devices", [])
                update_caseta_devices(ctx, devices)
                continue

            # Caseta scene discovery
            if typ == "lutron.scenes":
                scenes = data.get("scenes", [])
                update_caseta_scenes(ctx, scenes)
                continue

            # Watchdog health
            if typ == "watchdog.health":
                update_watchdog_health(ctx, data.get("services", {}))
                continue

            # Voice command
            if typ != "voice.command":
                continue

            text = str(data.get("text") or "").strip()
            room_id = str(data.get("room_id") or "")
            room_name = str(data.get("room_name") or room_id)
            log.info("voice_command_received", room_id=room_id, room_name=room_name, text=text[:80] if text else "")

            if not text:
                log.warning("voice_command_skipped", reason="empty_text", room_id=room_id)
                continue

            speakers = _speakers_for_room(room_id)
            _timing = {"t0": time.monotonic()}
            log.info("voice_command", room=room_name, text=text, speakers=speakers)

            try:
                # --- Check for pending confirmation ---
                if room_id in pending_actions:
                    pa = pending_actions[room_id]
                    if not pa.is_expired:
                        # Use fast LLM to interpret confirmation
                        confirm_result = await fast_llm.chat(
                            system="The user was asked to confirm an action. They said the following. Reply with exactly CONFIRM if they are agreeing/confirming, CANCEL if they are rejecting/cancelling, or NEW if this is an unrelated new command.",
                            user=text,
                            max_tokens=10,
                            temperature=0.0,
                        )
                        interpretation = confirm_result.text.strip().upper()
                        log.info("confirmation_check", room=room_name, text=text, interpretation=interpretation)

                        if "CONFIRM" in interpretation:
                            # Execute the pending action
                            evt = make_event(source="voice-intent-agent", typ=pa.mqtt_payload.get("type", "custom"),
                                data=pa.mqtt_payload)
                            mqttc.publish_json(pa.mqtt_topic, evt)
                            confirmation = "Done. %s" % pa.description
                            _respond(confirmation, room_id, room_name, speakers, _t0=_timing["t0"])
                            # Save as learned action
                            learned.save_action(
                                phrase=pa.original_text, room_id=room_id,
                                mqtt_topic=pa.mqtt_topic, mqtt_payload=pa.mqtt_payload,
                                description=pa.description,
                            )
                            log.info("pending_confirmed", room=room_name, description=pa.description)
                            del pending_actions[room_id]
                            continue

                        elif "CANCEL" in interpretation:
                            _respond("Cancelled.", room_id, room_name, speakers, _t0=_timing["t0"])
                            log.info("pending_cancelled", room=room_name)
                            del pending_actions[room_id]
                            continue

                        else:
                            # Treat as new command
                            del pending_actions[room_id]
                            log.info("pending_replaced", room=room_name)

                    else:
                        del pending_actions[room_id]

                # --- Check learned actions ---
                candidates = learned.find_candidates(room_id)
                if candidates:
                    # Use fast LLM to check for match
                    phrases = "\n".join("%d. %s" % (i+1, c.phrase) for i, c in enumerate(candidates[:10]))
                    match_result = await fast_llm.chat(
                        system="The user said something. Check if it matches any of these previously learned commands. Reply with JUST the number (1, 2, etc.) if there's a match, or NONE if no match. Context: home automation voice commands.",
                        user="User said: \"%s\"\n\nKnown commands:\n%s" % (text, phrases),
                        max_tokens=10,
                        temperature=0.0,
                    )
                    match_text = match_result.text.strip()
                    try:
                        match_idx = int(match_text) - 1
                        if 0 <= match_idx < len(candidates):
                            matched = candidates[match_idx]
                            log.info("learned_match", room=room_name, phrase=matched.phrase)
                            evt = make_event(source="voice-intent-agent",
                                typ=matched.mqtt_payload.get("type", "custom"),
                                data=matched.mqtt_payload)
                            mqttc.publish_json(matched.mqtt_topic, evt)
                            confirmation = "Done. %s" % matched.description
                            _respond(confirmation, room_id, room_name, speakers, _t0=_timing["t0"])
                            learned.record_use(matched)
                            continue
                    except (ValueError, IndexError):
                        pass

                # --- Build messages (stateless — each wake word is a fresh query) ---
                system_prompt = _BASE_SYSTEM_PROMPT + "\n\n" + ctx.build_prompt_context()
                messages = [
                    {"role": "user", "content": "[Room: %s] %s" % (room_name, text)},
                ]

                # --- Build tools ---
                tools = ctx.build_all_tools() + [_CUSTOM_ACTION_TOOL]

                # --- Call fast LLM ---
                result = await fast_llm.chat_with_tools(
                    messages=[{"role": "system", "content": system_prompt}] + messages,
                    tools=tools,
                    max_tokens=512,
                    temperature=0.3,
                )

                # --- Handle result ---
                if isinstance(result, LLMToolCall):
                    if result.name == "custom_action":
                        # Escalate to reasoning LLM if available
                        description = result.arguments.get("description", "")
                        mqtt_topic = result.arguments.get("mqtt_topic", "")
                        mqtt_payload = result.arguments.get("mqtt_payload", {})

                        if reasoning_llm and (not mqtt_topic or not mqtt_payload):
                            # Fast model couldn't construct the payload — ask Claude
                            _respond_ack("voice_reasoning", speakers)
                            reason_result = await reasoning_llm.chat_with_tools(
                                system=system_prompt + "\n\nConstruct the exact MQTT topic and payload for the user's request. Use custom_action.",
                                messages=messages,
                                tools=[_CUSTOM_ACTION_TOOL],
                                max_tokens=512,
                                temperature=0.2,
                            )
                            if isinstance(reason_result, LLMToolCall) and reason_result.name == "custom_action":
                                description = reason_result.arguments.get("description", description)
                                mqtt_topic = reason_result.arguments.get("mqtt_topic", mqtt_topic)
                                mqtt_payload = reason_result.arguments.get("mqtt_payload", mqtt_payload)
                            elif isinstance(reason_result, LLMTextResponse):
                                _respond(reason_result.text, room_id, room_name, speakers, _t0=_timing["t0"])
                                continue

                        if mqtt_topic and mqtt_payload:
                            # Store as pending, ask for confirmation
                            pending_actions[room_id] = PendingAction(
                                description=description,
                                mqtt_topic=mqtt_topic,
                                mqtt_payload=mqtt_payload,
                                original_text=text,
                                created_at=time.monotonic(),
                                room_id=room_id,
                                room_name=room_name,
                            )
                            confirm_text = "I can %s. Shall I go ahead?" % description
                            _respond(confirm_text, room_id, room_name, speakers, _t0=_timing["t0"])
                            log.info("pending_created", room=room_name, description=description)
                        else:
                            _respond("I'm not sure how to do that.", room_id, room_name, speakers, _t0=_timing["t0"])
                    else:
                        # Known tool — execute immediately
                        confirmation = await _execute_tool_call(result, room_id, room_name)
                        if confirmation:
                            _respond(confirmation, room_id, room_name, speakers, _t0=_timing["t0"])

                elif isinstance(result, LLMTextResponse):
                    answer = result.text
                    # If the fast model gave a text response and we have Perplexity,
                    # try to get a better web-searched answer — but only for general
                    # knowledge questions, not queries our local tools can handle.
                    _local_keywords = {"weather", "temperature", "forecast", "wind",
                                       "lights?", "scene", "camera", "sensor",
                                       "humidity", "ups", "battery", "briefing",
                                       "mute", "unmute", "announce", "time",
                                       "calendar", "schedule"}
                    _text_lower = text.lower()
                    _skip_perplexity = any(
                        re.search(r"\b" + kw + r"\b", _text_lower)
                        for kw in _local_keywords
                    )
                    if answer and perplexity_llm and not _skip_perplexity:
                        try:
                            pplx_answer = await perplexity_llm.chat(
                                system="You are a helpful assistant for a family in Lynchburg, Virginia. Answer concisely in one to three sentences. Format for spoken audio: spell out numbers, no URLs, no markdown.",
                                user=text,
                                max_tokens=256,
                                temperature=0.2,
                            )
                            if pplx_answer and len(pplx_answer.strip()) > 10:
                                pplx_answer = re.sub(r"\[\d+\]", "", pplx_answer)  # [1] [2] etc
                                pplx_answer = re.sub(r"(?<=\.)(\d{2,})", "", pplx_answer)  # .12367 trailing refs
                                pplx_answer = re.sub(r"(?<=\w)(\d{4,})", "", pplx_answer)  # word12367
                                pplx_answer = re.sub(r"\*\*", "", pplx_answer)  # **bold**
                                pplx_answer = re.sub(r"\s{2,}", " ", pplx_answer).strip()  # cleanup
                                answer = pplx_answer.strip()
                                log.info("perplexity_answer", room=room_name, answer=answer[:80])
                        except Exception as e:
                            log.warning("perplexity_failed", error=str(e)[:100])
                    if answer:
                        _respond(answer, room_id, room_name, speakers, _t0=_timing["t0"])

                elif isinstance(result, list):
                    # Multiple tool calls
                    responses = []
                    for tc in result:
                        if isinstance(tc, LLMToolCall):
                            r = await _execute_tool_call(tc, room_id, room_name)
                            if r:
                                responses.append(r)
                    if responses:
                        combined = " ".join(responses)
                        _respond(combined, room_id, room_name, speakers, _t0=_timing["t0"])

            except Exception as e:
                log.exception("intent_processing_failed", room=room_name, text=text)
                reporter.report_error("intent_processing_failed", e)
                try:
                    _respond("I'm sorry, I had trouble processing that request.", room_id, room_name, speakers, _t0=_timing["t0"])
                except Exception:
                    log.warning("error_response_failed", room=room_name)

    finally:
        timeout_task.cancel()
        status_task.cancel()
        await mqttc.close()


def main() -> int:
    asyncio.run(run_voice_intent_agent())
    return 0
