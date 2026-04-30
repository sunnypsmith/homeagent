"""Voice intent agent v4 — Claude agent loop architecture.

Pipeline: DB fuzzy match -> Groq classify -> Claude agent loop.
The agent has parameterized tools and can chain multiple calls to
answer complex questions. Successful runs are auto-saved to the DB
for future fuzzy matching.
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
from home_agent.integrations.llm import LLMClient, LLMToolCall
from home_agent.integrations.llm_router import LLMRouter
from home_agent.services.agent_tools import ToolExecutor, build_tools
from home_agent.services.voice_system_context import SystemContext
from home_agent.services.voice_registrations import (
    register_all, update_caseta_devices, update_caseta_scenes, update_watchdog_health,
)

_AGENT_SYSTEM = """You are Higgins, a helpful home assistant for the Smith family in Lynchburg, Virginia.

Use the available tools to answer questions and execute commands. You may call multiple tools if needed.

Format ALL output for spoken text-to-speech audio:
- Spell out numbers, times, dates, currency, percentages, and units as full words.
- No URLs, markdown, bullet points, or special characters.
- Use short, natural sentences. You are speaking out loud to a person in their home.
- Be concise."""


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
        host=settings.mqtt.host, port=settings.mqtt.port,
        username=settings.mqtt.username, password=settings.mqtt.password,
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

    # --- DB ---
    db = DbManager(
        conninfo=settings.db.conninfo,
        log_info=DbConnectInfo(host=settings.db.host, port=settings.db.port, dbname=settings.db.name, user=settings.db.user),
        connect_timeout_seconds=10.0, reconnect_max_wait_seconds=30.0,
    )
    intent_store = IntentStore(db)

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

    # Claude for agent loop
    from home_agent.integrations.llm_anthropic import AnthropicClient
    claude = None
    if settings.voice_reasoning_api_key:
        claude = AnthropicClient(
            api_key=settings.voice_reasoning_api_key,
            model=settings.voice_reasoning_model,
            timeout_seconds=settings.voice_reasoning_timeout,
        )
        log.info("claude_agent", model=settings.voice_reasoning_model)

    perplexity_llm = None
    if settings.voice_perplexity_api_key:
        perplexity_llm = LLMClient(
            base_url="https://api.perplexity.ai",
            api_key=settings.voice_perplexity_api_key,
            model=settings.voice_perplexity_model,
            timeout_seconds=settings.voice_perplexity_timeout,
        )

    # --- System context (still used for Caseta device discovery via MQTT) ---
    ctx = SystemContext()
    register_all(ctx, settings, mqttc)

    # --- Agent tools ---
    agent_tools = build_tools(settings)
    tool_executor = ToolExecutor(settings, mqttc, ctx, perplexity_llm=perplexity_llm)
    log.info("agent_tools", count=len(agent_tools),
             names=[t["function"]["name"] for t in agent_tools])

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

    # --- Background tasks ---

    async def _timeout_loop() -> None:
        while True:
            await asyncio.sleep(10.0)
            for rid in [r for r, pa in pending_actions.items() if pa.is_expired]:
                pending_actions.pop(rid)
                _respond_ack("voice_cancelled", _speakers_for_room(rid))

    async def _status_loop() -> None:
        while True:
            await asyncio.sleep(60.0)
            log.info("status", mqtt_connected=mqttc.is_connected,
                     pending=len(pending_actions), learned_intents=intent_store.count())

    timeout_task = asyncio.create_task(_timeout_loop())
    status_task = asyncio.create_task(_status_loop())

    # --- Main loop ---
    try:
        while True:
            msg = await mqttc.next_message()
            try:
                payload = msg.json()
            except Exception as e:
                log.warning("mqtt_decode_failed", error=str(e)[:100])
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
                continue

            speakers = _speakers_for_room(room_id)
            t0 = time.monotonic()

            try:
                # ── Pending confirmation check ──
                if room_id in pending_actions:
                    pa = pending_actions[room_id]
                    if not pa.is_expired:
                        cr = await fast_llm.chat(
                            system="The user was asked to confirm an action. Reply with exactly CONFIRM, CANCEL, or NEW.",
                            user=text, max_tokens=10, temperature=0.0,
                        )
                        interp = cr.text.strip().upper()
                        if "CONFIRM" in interp:
                            mqttc.publish_json(pa.mqtt_topic,
                                make_event(source="voice-intent-agent",
                                    typ=pa.mqtt_payload.get("type", "custom"), data=pa.mqtt_payload))
                            _respond("Done. %s" % pa.description, room_id, room_name, speakers, _t0=t0)
                            intent_store.save(
                                phrase=pa.original_text, category="custom",
                                mqtt_topic=pa.mqtt_topic, mqtt_payload=pa.mqtt_payload,
                                description=pa.description,
                            )
                            del pending_actions[room_id]
                            continue
                        elif "CANCEL" in interp:
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
                    log.info("db_match", phrase=match.phrase, category=match.category, id=match.id)
                    if match.tool_name:
                        result_text = await tool_executor.execute(match.tool_name, match.tool_args)
                        if claude:
                            formatted = await claude.chat(
                                system=_AGENT_SYSTEM,
                                user="The user asked: '%s'. The data is: %s. Give a concise spoken response." % (text, result_text),
                                max_tokens=256, temperature=0.2,
                            )
                            _respond(formatted, room_id, room_name, speakers, _t0=t0)
                        else:
                            _respond(result_text, room_id, room_name, speakers, _t0=t0)
                    elif match.mqtt_topic and match.mqtt_payload:
                        mqttc.publish_json(match.mqtt_topic,
                            make_event(source="voice-intent-agent",
                                typ=match.mqtt_payload.get("type", "custom"), data=match.mqtt_payload))
                        _respond("Done. %s" % match.description, room_id, room_name, speakers, _t0=t0)
                    intent_store.record_use(match.id)
                    continue

                # ── Step 2: Claude agent ──
                if claude:
                    result = await claude.agent_loop(
                        system=_AGENT_SYSTEM,
                        user="[Room: %s] %s" % (room_name, text),
                        tools=agent_tools,
                        execute_tool=tool_executor.execute,
                        max_tokens=512,
                        temperature=0.2,
                    )
                    log.info("agent_complete", turns=result.turns,
                             tools=[tc.name for tc in result.tool_calls])

                    if result.text:
                        _respond(result.text, room_id, room_name, speakers, _t0=t0)

                    # Auto-learn: save the first tool call for fuzzy matching
                    if result.tool_calls:
                        first_tc = result.tool_calls[0]
                        intent_store.save(
                            phrase=text,
                            category=first_tc.name,
                            tool_name=first_tc.name,
                            tool_args=first_tc.arguments,
                            description=result.text[:100] if result.text else "",
                        )
                else:
                    _respond("I'm sorry, the reasoning system is not available right now.",
                             room_id, room_name, speakers, _t0=t0)

            except Exception as e:
                log.exception("intent_processing_failed", room=room_name, text=text)
                reporter.report_error("intent_processing_failed", e)
                try:
                    _respond("I'm sorry, I had trouble processing that request.",
                             room_id, room_name, speakers, _t0=t0)
                except Exception:
                    pass

    finally:
        timeout_task.cancel()
        status_task.cancel()
        db.close()
        await mqttc.close()


def main() -> int:
    asyncio.run(run_voice_intent_agent())
    return 0
