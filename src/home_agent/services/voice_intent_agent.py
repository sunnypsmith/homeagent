"""Voice intent agent v3 — classify-then-route architecture.

Pipeline: DB fuzzy match -> LLM intent classify -> targeted tool route.
Each step uses minimal LLM payloads (no tools for classify, 1-3 tools for route).
Custom actions are confirmed, then persisted to DB for future fuzzy matching.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from home_agent.bus.envelope import make_event
from home_agent.bus.error_reporter import ErrorReporter
from home_agent.bus.mqtt_client import MqttClient
from home_agent.config import AppSettings
from home_agent.core.logging import configure_logging, get_logger
from home_agent.db import DbConnectInfo, DbManager
from home_agent.integrations.intent_store import IntentStore
from home_agent.integrations.llm import LLMClient, LLMToolCall, LLMTextResponse
from home_agent.integrations.llm_router import LLMRouter
from home_agent.services.intent_classifier import classify
from home_agent.services.intent_router import route
from home_agent.services.voice_system_context import SystemContext
from home_agent.services.voice_registrations import (
    register_all, update_caseta_devices, update_caseta_scenes, update_watchdog_health,
)


_CUSTOM_ACTION_TOOL = {
    "type": "function",
    "function": {
        "name": "custom_action",
        "description": (
            "For actions not covered by other tools. Construct the correct MQTT topic and payload."
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


async def run_voice_intent_agent() -> None:
    settings = AppSettings()
    configure_logging(settings.log_level)
    log = get_logger(service="voice_intent_agent")

    # --- MQTT ---
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

    base = settings.mqtt.base_topic
    mqttc.subscribe("%s/voice/command" % base)
    mqttc.subscribe("%s/lutron/event" % base)
    mqttc.subscribe("%s/watchdog/health" % base)
    log.info("subscribed")

    # --- DB (for learned intents) ---
    db = DbManager(
        conninfo=settings.db.conninfo,
        log_info=DbConnectInfo(host=settings.db.host, port=settings.db.port, dbname=settings.db.name, user=settings.db.user),
        connect_timeout_seconds=10.0,
        reconnect_max_wait_seconds=30.0,
    )
    intent_store = IntentStore(db)
    log.info("intent_store_ready")

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

    # Tool LLM: use the fallback (OpenAI) as primary for tool calls since
    # Groq/Llama has unreliable structured tool calling (~4% XML failures).
    if settings.llm_fallback.enabled:
        tool_providers = [
            ("primary", LLMClient(
                base_url=settings.llm_fallback.base_url, api_key=settings.llm_fallback.api_key,
                model=settings.llm_fallback.model, timeout_seconds=settings.llm_fallback.timeout_seconds,
            )),
        ]
    else:
        tool_providers = list(fast_providers)
    tool_llm = LLMRouter(tool_providers)
    log.info("tool_llm", providers=[p[0] for p in tool_providers])

    reasoning_llm = None
    if settings.voice_reasoning_api_key:
        from home_agent.integrations.llm_anthropic import AnthropicClient
        reasoning_llm = AnthropicClient(
            api_key=settings.voice_reasoning_api_key,
            model=settings.voice_reasoning_model,
            timeout_seconds=settings.voice_reasoning_timeout,
        )

    perplexity_llm = None
    if settings.voice_perplexity_api_key:
        perplexity_llm = LLMClient(
            base_url="https://api.perplexity.ai",
            api_key=settings.voice_perplexity_api_key,
            model=settings.voice_perplexity_model,
            timeout_seconds=settings.voice_perplexity_timeout,
        )

    # --- System context ---
    ctx = SystemContext()
    register_all(ctx, settings, mqttc)

    # --- State ---
    room_speakers = settings.voice_room_speakers_parsed
    pending_actions: Dict[str, PendingAction] = {}
    log.info("ready", queries=len(ctx.query_names), actions=len(ctx.action_names),
             room_speakers=room_speakers, learned_intents=intent_store.count())

    response_topic = "%s/voice/response" % base

    # --- Helpers ---
    def _speakers_for_room(room_id: str) -> Optional[List[str]]:
        return room_speakers.get(room_id)

    def _respond(text: str, room_id: str, room_name: str, speakers: Optional[List[str]], _t0: Optional[float] = None) -> None:
        if _t0 is not None:
            log.info("intent_respond", room=room_name, text_len=len(text),
                     intent_elapsed_ms=round((time.monotonic() - _t0) * 1000))
        evt = make_event(source="voice-intent-agent", typ="voice.response",
            data={"room_id": room_id, "room_name": room_name, "text": text})
        mqttc.publish_json(response_topic, evt)
        if speakers and speakers != ["none"]:
            data: Dict[str, Any] = {"text": text, "targets": speakers, "exempt_mute": True, "exempt_quiet_hours": True}
            mqttc.publish_json("%s/announce/request" % base,
                make_event(source="voice-intent-agent", typ="announce.request", data=data))

    def _respond_ack(key: str, speakers: Optional[List[str]]) -> None:
        if speakers and speakers != ["none"]:
            data: Dict[str, Any] = {"text": key, "offline_audio_key": key, "targets": speakers, "exempt_mute": True, "exempt_quiet_hours": True}
            mqttc.publish_json("%s/announce/request" % base,
                make_event(source="voice-intent-agent", typ="announce.request", data=data))

    async def _execute_tool_call(tc: LLMToolCall, room_id: str, room_name: str) -> Optional[str]:
        name = tc.name
        args = tc.arguments
        log.info("tool_execute", tool=name, args=args)

        if name == "query_system":
            return await ctx.execute_query(args.get("query", ""))

        if name == "announce":
            ann_text = args.get("text", "")
            if not ann_text:
                return "I need some text to announce."
            ann_data: Dict[str, Any] = {"text": ann_text}
            if args.get("targets"):
                ann_data["targets"] = args["targets"]
            mqttc.publish_json("%s/announce/request" % base,
                make_event(source="voice-intent-agent", typ="announce.request", data=ann_data))
            return "Done. I've made the announcement."

        if name == "mute_announcements":
            minutes = int(args.get("minutes", 60))
            muted_until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
            mqttc.publish_json("%s/announce/mute" % base,
                make_event(source="voice-intent-agent", typ="announce.mute",
                    data={"muted_until_unix": int(muted_until.timestamp()), "duration_minutes": minutes}),
                retain=True)
            return "Done. Announcements muted for %d minutes." % minutes

        if name in ("lights_on", "lights_off", "lights_level", "activate_scene"):
            action_map = {"lights_on": "on", "lights_off": "off", "lights_level": "level", "activate_scene": "scene"}
            lutron_data: Dict[str, Any] = {"action": action_map[name]}
            if "device_id" in args:
                lutron_data["device_id"] = int(args["device_id"])
            if "level" in args:
                lutron_data["level"] = int(args["level"])
            if "scene_name" in args:
                lutron_data["scene_name"] = str(args["scene_name"])
            mqttc.publish_json("%s/lutron/command" % base,
                make_event(source="voice-intent-agent", typ="lutron.command", data=lutron_data))
            if name == "activate_scene":
                return "Done. I've activated the %s scene." % args.get("scene_name", "")
            return "Done."

        if name.endswith("_command"):
            cmd = args.get("command", "")
            reg = ctx.find_category_action(name, cmd)
            if reg:
                return await ctx.execute_action(cmd)
            return "I didn't recognize that command."

        reg = ctx.find_action(name)
        if reg:
            return await ctx.execute_action(name)
        return "I executed the command."

    # --- Background tasks ---

    async def _timeout_loop() -> None:
        while True:
            await asyncio.sleep(10.0)
            expired = [rid for rid, pa in pending_actions.items() if pa.is_expired]
            for rid in expired:
                pa = pending_actions.pop(rid)
                _respond_ack("voice_cancelled", _speakers_for_room(rid))
                log.info("pending_expired", room=pa.room_name)

    async def _status_loop() -> None:
        while True:
            await asyncio.sleep(60.0)
            log.info("status", mqtt_connected=mqttc.is_connected,
                     pending=len(pending_actions),
                     learned_intents=intent_store.count())

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

            if typ == "lutron.devices":
                update_caseta_devices(ctx, data.get("devices", []))
                continue
            if typ == "lutron.scenes":
                update_caseta_scenes(ctx, data.get("scenes", []))
                continue
            if typ == "watchdog.health":
                update_watchdog_health(ctx, data.get("services", {}))
                continue
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
            t0 = time.monotonic()
            log.info("voice_command", room=room_name, text=text, speakers=speakers)

            try:
                # ── Step 0: Pending confirmation check ──
                if room_id in pending_actions:
                    pa = pending_actions[room_id]
                    if not pa.is_expired:
                        confirm_result = await fast_llm.chat(
                            system="The user was asked to confirm an action. Reply with exactly CONFIRM, CANCEL, or NEW.",
                            user=text, max_tokens=10, temperature=0.0,
                        )
                        interpretation = confirm_result.text.strip().upper()
                        log.info("confirmation_check", room=room_name, interpretation=interpretation)

                        if "CONFIRM" in interpretation:
                            mqttc.publish_json(pa.mqtt_topic,
                                make_event(source="voice-intent-agent",
                                    typ=pa.mqtt_payload.get("type", "custom"), data=pa.mqtt_payload))
                            _respond("Done. %s" % pa.description, room_id, room_name, speakers, _t0=t0)
                            intent_store.save(
                                phrase=pa.original_text, category="custom",
                                mqtt_topic=pa.mqtt_topic, mqtt_payload=pa.mqtt_payload,
                                description=pa.description,
                            )
                            log.info("pending_confirmed", room=room_name, description=pa.description)
                            del pending_actions[room_id]
                            continue

                        elif "CANCEL" in interpretation:
                            _respond("Cancelled.", room_id, room_name, speakers, _t0=t0)
                            del pending_actions[room_id]
                            continue
                        else:
                            del pending_actions[room_id]
                    else:
                        del pending_actions[room_id]

                # ── Step 1: DB fuzzy match ──
                match = intent_store.find_match(text)
                if match:
                    log.info("db_match", phrase=match.phrase, category=match.category,
                             tool=match.tool_name, sim_id=match.id)
                    if match.mqtt_topic and match.mqtt_payload:
                        mqttc.publish_json(match.mqtt_topic,
                            make_event(source="voice-intent-agent",
                                typ=match.mqtt_payload.get("type", "custom"), data=match.mqtt_payload))
                        _respond("Done. %s" % match.description, room_id, room_name, speakers, _t0=t0)
                    elif match.tool_name:
                        tc = LLMToolCall(name=match.tool_name, arguments=match.tool_args)
                        result_text = await _execute_tool_call(tc, room_id, room_name)
                        if result_text:
                            _respond(result_text, room_id, room_name, speakers, _t0=t0)
                    intent_store.record_use(match.id)
                    continue

                # ── Step 2: Classify intent (fast LLM, no tools) ──
                intent = await classify(text, room_name, fast_llm)
                log.info("intent_classified", category=intent.category)

                # ── Step 3: Route to targeted handler ──
                if intent.category == "custom":
                    # Custom actions need the reasoning LLM + confirmation flow
                    system_prompt = (
                        "You are Higgins, a home assistant. Construct the MQTT topic and payload "
                        "for the user's request.\n\n" + ctx.build_prompt_context()
                    )
                    messages = [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": "[Room: %s] %s" % (room_name, text)},
                    ]
                    llm_for_custom = reasoning_llm or fast_llm
                    if reasoning_llm:
                        _respond_ack("voice_reasoning", speakers)
                        custom_result = await reasoning_llm.chat_with_tools(
                            system=system_prompt,
                            messages=messages,
                            tools=[_CUSTOM_ACTION_TOOL],
                            max_tokens=512, temperature=0.2,
                        )
                    else:
                        custom_result = await fast_llm.chat_with_tools(
                            messages=messages,
                            tools=[_CUSTOM_ACTION_TOOL],
                            max_tokens=512, temperature=0.2,
                        )

                    if isinstance(custom_result, LLMToolCall) and custom_result.name == "custom_action":
                        description = custom_result.arguments.get("description", "")
                        mqtt_topic = custom_result.arguments.get("mqtt_topic", "")
                        mqtt_payload = custom_result.arguments.get("mqtt_payload", {})
                        if mqtt_topic and mqtt_payload:
                            pending_actions[room_id] = PendingAction(
                                description=description, mqtt_topic=mqtt_topic,
                                mqtt_payload=mqtt_payload, original_text=text,
                                created_at=time.monotonic(), room_id=room_id, room_name=room_name,
                            )
                            _respond("I can %s. Shall I go ahead?" % description, room_id, room_name, speakers, _t0=t0)
                            log.info("pending_created", room=room_name, description=description)
                        else:
                            _respond("I'm not sure how to do that.", room_id, room_name, speakers, _t0=t0)
                    elif isinstance(custom_result, LLMTextResponse) and custom_result.text:
                        _respond(custom_result.text, room_id, room_name, speakers, _t0=t0)
                    else:
                        _respond("I'm not sure how to do that.", room_id, room_name, speakers, _t0=t0)
                else:
                    result = await route(
                        category=intent.category, text=text, room_name=room_name,
                        ctx=ctx, fast_llm=tool_llm, perplexity_llm=perplexity_llm,
                        execute_tool_fn=_execute_tool_call,
                    )
                    if isinstance(result, LLMToolCall):
                        result_text = await _execute_tool_call(result, room_id, room_name)
                        if result_text:
                            _respond(result_text, room_id, room_name, speakers, _t0=t0)
                    elif isinstance(result, str) and result:
                        _respond(result, room_id, room_name, speakers, _t0=t0)
                    elif result is None:
                        _respond("I'm not sure how to help with that.", room_id, room_name, speakers, _t0=t0)

            except Exception as e:
                log.exception("intent_processing_failed", room=room_name, text=text)
                reporter.report_error("intent_processing_failed", e)
                try:
                    _respond("I'm sorry, I had trouble processing that request.", room_id, room_name, speakers, _t0=t0)
                except Exception:
                    log.warning("error_response_failed", room=room_name)

    finally:
        timeout_task.cancel()
        status_task.cancel()
        db.close()
        await mqttc.close()


def main() -> int:
    asyncio.run(run_voice_intent_agent())
    return 0
