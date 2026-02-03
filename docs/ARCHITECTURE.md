# Architecture

## Goals

- **Always-on** process that can schedule and react all day
- **Modular**: add/remove behaviors as isolated modules
- **Operable**: logs, clear separation of concerns, easy debugging
- **Integration-first**: LLM + local/external APIs + Sonos announcements

## Current architecture (service-based)

The current design favors **small always-on services** that communicate over **MQTT** (instead of a single in-process app).

### Event envelope (strict)
All messages on MQTT use the same JSON envelope:
- `id`, `ts`, `source`, `type`, `trace_id`, `data`

### Core services
- **`sonos-gateway`**: consumes `announce.request`, generates TTS audio, hosts it, and plays it on Sonos  
  - **Quiet hours are enforced here** (announcements are suppressed during quiet hours).
- **`time-trigger`**: loads schedules from Postgres (`schedules` table) and publishes time events to MQTT
- **`event-recorder`**: subscribes to MQTT topics and records all events to TimescaleDB (`events` table)
- **`ui-gateway`** (optional): simple LAN web UI with buttons that publish MQTT `announce.request` events

### Agents (examples)
- **`wakeup-agent`**: consumes `time.cron.wakeup_call` and emits `announce.request`
- **`morning-briefing-agent`**: consumes `time.cron.morning_briefing`, calls LLM + weather (+ optional calendar ICS), emits `announce.request`
- **`hourly-chime-agent`**: consumes `time.cron.hourly_chime` and emits `announce.request`
- **`fixed-announcement-agent`**: consumes `time.cron.fixed_announcement` and emits `announce.request` using `data.text`

## Legacy concepts (in-process)
Some earlier code/doc concepts refer to a single `HomeAgentApp` + in-process `EventBus` modules. They’re still useful patterns, but the active path is the service-based stack above.

## Key concepts (legacy)

### `HomeAgentApp`
Owns process lifecycle (start/stop), builds dependencies, starts modules, runs until SIGINT/SIGTERM.

### `Scheduler`
Single place for time-based behaviors:
- interval jobs (e.g. every 60s)
- cron jobs (e.g. 08:00 daily)

### `EventBus`
Lightweight async pub/sub so modules can communicate without tight coupling:
- modules publish events (e.g. `briefing.sent`)
- other modules subscribe to react (e.g. log, persist, notify, etc.)

### `Module`
Pluggable unit of behavior. A module’s `start(ctx)` typically:
- registers scheduled jobs
- subscribes to events
- calls integrations to do real-world actions

## Integrations

### LLM
`integrations/llm.py` is an OpenAI-compatible `/v1/chat/completions` client.

### Sonos
Sonos output is handled by the dedicated `sonos-gateway` service:
- subscribes to `homeagent/announce/request`
- generates TTS audio (ElevenLabs)
- hosts the audio over HTTP on the LAN
- plays it on Sonos (SoCo), then restores the previous state

Announcements can optionally include `data.targets` (aliases or IPs) to direct
playback to specific speakers.

The common “true speech on Sonos” pipeline is:
1) call a TTS API to generate audio bytes
2) host audio on a local HTTP endpoint
3) have Sonos play the audio URL (and restore the previous queue/state)

Quiet hours are **hard-enforced** in `sonos-gateway`.
