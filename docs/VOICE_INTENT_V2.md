# Voice Intent Agent v2 — Architecture Guide

## Overview

The voice intent agent was rebuilt from a hard-coded tool dispatch system to a registry-based adaptive architecture. This document explains the design for anyone forking or extending the system.

## The Problem with v1

In v1, every capability was hard-coded in multiple places:

- **Tool definitions**: a static `TOOLS` list with hand-written JSON schemas
- **Tool descriptions**: hard-coded device names, scene names, command lists
- **Execution dispatch**: a long `if/elif` chain matching tool names to handlers
- **Confirmation text**: separate dicts mapping commands to response strings

Adding a new capability (e.g., a thermostat) required editing 4-6 places and remembering to keep them in sync. Removing a capability meant finding every reference. The LLM only knew about what was explicitly listed.

## The Registry Pattern

v2 replaces all of this with two registries managed by `SystemContext`:

```
SystemContext
├── Query Registry      (data sources you can ask about)
│   ├── time            → query_fn, context_fn
│   ├── weather_current → query_fn, context_fn
│   ├── ups             → query_fn, context_fn
│   ├── tempstick       → query_fn, context_fn
│   └── ...
│
├── Action Registry     (things you can command)
│   ├── activate_scene  → handler, confirmation, parameters
│   ├── lights_on       → handler, confirmation, parameters
│   ├── household_dinner→ handler, confirmation
│   ├── trigger_morning → handler, confirmation
│   └── ...
│
└── Dynamic Context     (updated live from MQTT)
    ├── caseta_devices  → device names + IDs
    ├── caseta_scenes   → scene names
    └── watchdog_health → service status
```

### Registration

Each integration registers itself once on startup:

```python
# Query registration (data source)
ctx.register_query(
    name="ups",
    description="UPS input voltage and frequency",
    query_fn=_query_ups,           # async callable -> str
    context_fn=lambda: "UPS: Main UPS at 10.1.2.59",  # for system prompt
)

# Action registration (executable command)
ctx.register_action(
    name="activate_scene",
    description="Activate a Caseta lighting scene",
    handler=_activate_scene,        # async callable
    confirmation="Done.",           # TTS response after execution
    category="lighting",
    parameters={...},               # OpenAI tool schema (optional)
)
```

### What the Registry Auto-Generates

From these registrations, the system automatically builds:

1. **LLM tool definitions** — `ctx.build_all_tools()` returns the complete OpenAI-format tool list. Query names become the `query_system` enum values. Actions become individual tools or grouped by category.

2. **System prompt context** — `ctx.build_prompt_context()` returns a text block listing all available data sources, actions, and MQTT architecture. Injected into the LLM system prompt so it knows what the home can do.

3. **Execution routing** — `ctx.execute_query(name)` and `ctx.execute_action(name)` dispatch to the registered handler. No if/elif chains.

### Adding a New Integration

To add a new capability (e.g., a pool temperature sensor):

```python
# In voice_registrations.py, inside register_all():

if settings.pool.enabled:
    async def _query_pool() -> str:
        client = PoolClient(host=settings.pool.host)
        reading = await client.get_temperature()
        return "Pool temperature is %d degrees." % int(round(reading.temp_f))

    ctx.register_query("pool", "Pool water temperature",
        _query_pool,
        context_fn=lambda: "Pool sensor: %s" % settings.pool.host)
```

That's it. The query_system tool, system prompt, and routing all update automatically. No other files need changing.

### Dynamic Registration from MQTT

Some capabilities are discovered at runtime. The Caseta bridge publishes its device and scene lists as retained MQTT messages. The intent agent subscribes and re-registers:

```python
# When lutron.scenes arrives via MQTT:
def update_caseta_scenes(ctx, scenes):
    scene_names = [s["name"] for s in scenes]
    ctx.update_dynamic_context("caseta_scenes",
        "CASETA SCENES: %s" % ", ".join(scene_names))
    # Update the activate_scene tool description
    reg = ctx.find_action("activate_scene")
    reg.parameters["properties"]["scene_name"]["description"] = \
        "Available: %s" % ", ".join(scene_names)
```

The LLM always sees the current device/scene list, even if devices are added or removed from the Caseta bridge.

## Three-Tier LLM Architecture

The intent agent uses three LLM providers, each for a specific purpose:

```
User speaks → STT → voice.command
                        │
                   Fast LLM (Groq)
                   "Route this to the right tool"
                        │
              ┌─────────┼──────────┐
              │         │          │
         Known tool  custom_action  Text response
         (execute)      │          │
                   Claude 4.6   Perplexity
                   "Build the    "Search the web
                    MQTT payload"  for an answer"
```

- **Groq / Kimi K2** (fast, free): initial tool dispatch. Decides whether the request maps to a registered tool, needs custom reasoning, or is a general question.
- **Claude 4.6** (reasoning): only called for `custom_action`. Receives the full system context (MQTT topics, device list, etc.) and constructs the correct MQTT topic + payload.
- **Perplexity Sonar** (web search): called when the fast model returns a text response (no tool match). Provides real-time web-searched answers for questions outside the local system.

### Why Three Tiers?

- Most requests (80%+) are handled by Groq in under 1 second. No expensive model needed.
- Custom actions are rare and need strong reasoning — Claude is the best at structured MQTT payloads.
- General knowledge questions ("Bitcoin price?", "weather in Costa Rica?") need web access — only Perplexity can do this.

## Per-Room Conversation Context

Each room (identified by `room_id` from the voice service) maintains a rolling conversation history in memory:

```python
@dataclass
class RoomConversation:
    messages: List[Dict]   # [{role: "user", content: "..."}, ...]
    last_activity: float

# Per-room dict
conversations: Dict[str, RoomConversation] = {}
```

- **Rolling 20 turns** (40 messages) per room. Oldest dropped.
- **30-minute stale timeout** — if no activity, history clears.
- **Midnight reset** — all rooms cleared via background task.
- **Per-room isolation** — office conversation doesn't leak into kitchen.
- **Passed to every LLM call** as message history, enabling contextual follow-ups.

## Confirmation Flow for Custom Actions

When the LLM can't find a registered tool and calls `custom_action`:

1. Intent agent plays "Let me think about that" (pre-recorded audio)
2. Claude constructs the MQTT topic and payload
3. Intent agent announces: "I can [description]. Shall I go ahead?"
4. Action is stored as a `PendingAction` for that room
5. User says wake word + "yes" / "go ahead"
6. Fast LLM classifies as CONFIRM → action executes
7. Confirmed action is saved to `data/learned_actions.json`
8. Next time the same request comes in, it matches as learned and executes instantly

Pending actions expire after 60 seconds with "Never mind, cancelled."

## Learned Actions

Confirmed custom actions are saved to a JSON file:

```json
[
    {
        "phrase": "dim the office lights to thirty percent",
        "room_id": "offi",
        "mqtt_topic": "homeagent/lutron/command",
        "mqtt_payload": {"action": "level", "device_id": 10, "level": 30},
        "description": "Set office light to 30 percent brightness",
        "confirmed_at": "2026-03-05T12:00:00Z",
        "use_count": 3,
        "last_used_at": "2026-03-06T08:00:00Z"
    }
]
```

Before calling the main LLM, the intent agent checks learned actions using a fast LLM phrase-matching call. If a match is found, the action executes immediately — no confirmation needed, no Claude call, no latency.

Over time, the system learns the user's patterns and gets faster.

## Web Chat Interface

The `/chat` page provides a text-based interface to the same intent agent:

- Publishes `voice.command` events with `room_id: "web"`
- Intent agent publishes responses to `homeagent/voice/response` topic
- Web room maps to `speakers: none` — no Sonos playback, text only
- Same conversation context, same tools, same reasoning

## File Structure

```
src/home_agent/
├── integrations/
│   ├── llm.py                    # OpenAI-compatible client (Groq, OpenAI)
│   ├── llm_anthropic.py          # Claude client (Anthropic SDK)
│   ├── llm_router.py             # Provider failover wrapper
│   └── learned_actions.py        # JSON-based learned actions store
│
├── services/
│   ├── voice_system_context.py   # Registry: queries + actions + dynamic context
│   ├── voice_registrations.py    # All integration registrations
│   ├── voice_intent_agent.py     # Main agent: message loop, LLM calls, dispatch
│   └── voice_service.py          # Audio: wake word, VAD, STT, UDP
```

## MQTT Topics

| Topic | Direction | Purpose |
|-------|-----------|---------|
| `homeagent/voice/command` | voice-service → intent-agent | Transcribed voice command |
| `homeagent/voice/response` | intent-agent → ui-gateway | Text response (for web chat) |
| `homeagent/announce/request` | intent-agent → sonos-gateway | Spoken response on Sonos |
| `homeagent/sonos/playback` | sonos-gateway → voice-service | Playback start/done (DEAF state) |
| `homeagent/lutron/event` | caseta-agent → intent-agent | Device/scene discovery |
| `homeagent/watchdog/health` | watchdog → intent-agent | Service health for context |
