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
  - **Quiet hours and mute are enforced here** (announcements are suppressed during quiet hours or while muted).
- **`time-trigger`**: loads schedules from Postgres (`schedules` table) and publishes time events to MQTT
- **`event-recorder`**: subscribes to MQTT topics and records all events to TimescaleDB (`events` table)
- **`ui-gateway`** (optional): LAN web UI with controls (`/`) and a real-time system status dashboard (`/status`) showing service health, MQTT activity, DB events, voice room status, recent voice commands, and errors
- **`watchdog`**: monitors all services via heartbeats and error events, announces failures, and attempts restarts via tmux (`WATCHDOG_TMUX_MAP`)
- **`voice-service`** (optional): receives UDP audio from M5Stack Atom Echo devices, runs Porcupine wake-word detection, WebRTC VAD, and an audio processing pipeline (noise reduction, silence trimming, peak normalization) before Groq Whisper STT; publishes `voice.command` events to MQTT. Goes "deaf" during Sonos playback to prevent feedback loops.
- **`voice-intent-agent`** (optional): subscribes to `voice.command`, uses LLM with tool calling to classify commands vs questions, dispatches actions via MQTT, and speaks responses through the room's Sonos speaker

### Agents (examples)
- **`wakeup-agent`**: consumes `time.cron.wakeup_call` and emits `announce.request`
- **`morning-briefing-agent`**: consumes `time.cron.morning_briefing`, calls LLM + weather (+ optional calendar ICS), emits `announce.request`
- **`hourly-chime-agent`**: consumes `time.cron.hourly_chime` and emits `announce.request`
- **`fixed-announcement-agent`**: consumes `time.cron.fixed_announcement` and emits `announce.request` using `data.text`
- **`hourly-house-check-agent`**: consumes `time.cron.hourly_house_check` and emits `house.check.report` (+ optional announcements)
- **`exec-briefing-agent`**: consumes `time.cron.exec_briefing` and emits `announce.request` (weather + calendar + financial)
- **`camect-agent`**: connects to Camect hub, publishes `camera.event` events, and optionally emits vision-enriched `announce.request`
- **`caseta-agent`**: bridges Lutron Caseta to MQTT for lighting control
- **`camera-lighting-agent`**: reacts to `camera.event` by triggering Caseta lighting scenes

### Voice pipeline
The voice system is split into two services:
1. **`voice-service`**: runs one Porcupine wake-word engine instance per room, each fed 16 kHz PCM frames from an Atom Echo over UDP. On wake detection, it plays a prompt ("How may I assist you?"), captures speech (WebRTC VAD with a 1-second pre-capture buffer to avoid clipping the first word), processes the audio (noise reduction via spectral gating, silence trimming, peak normalization to ~80% of int16 range), transcribes via Groq Whisper STT, and publishes `voice.command` to MQTT.
2. **`voice-intent-agent`**: receives `voice.command`, builds an LLM prompt with tool definitions (announce, mute, lights, scenes, briefings, household commands, time/weather), and either executes a tool call or responds conversationally. For general knowledge questions, Perplexity is used as a web-search fallback (skipped for queries that match local tools). Responses are targeted to the room's Sonos speaker via `VOICE_ROOM_SPEAKERS`.

Feedback suppression: when the Sonos gateway starts playback it publishes `sonos.playback_start`; the voice service goes "deaf" (drops audio buffers) until `sonos.playback_done` arrives, preventing the mic from picking up its own announcements.

ESPHome firmware for the Atom Echo devices lives in `esphome/`. Each device streams raw 16 kHz PCM via UDP with a 4-byte room-ID header, reports button press/release and online/offline status via MQTT, and accepts LED color commands.

### Database retention
The `events` hypertable has a 30-day TimescaleDB retention policy that automatically prunes old rows.

## Legacy concepts (in-process)
Some earlier code/doc concepts refer to a single `HomeAgentApp` + in-process `EventBus` modules. They're still useful patterns, but the active path is the service-based stack above.

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
Pluggable unit of behavior. A module's `start(ctx)` typically:
- registers scheduled jobs
- subscribes to events
- calls integrations to do real-world actions

## Integrations

### LLM
`integrations/llm.py` is an OpenAI-compatible `/v1/chat/completions` client. Supports tool calling (used by `voice-intent-agent`). Defaults to `temperature=0.3` and `max_tokens=1024` when callers don't specify — prevents accidental high-randomness outputs. The Anthropic client (`llm_anthropic.py`) has matching defaults.

### LLM Fallback Router
`integrations/llm_router.py` wraps one or more LLM providers with automatic failover. If the primary provider fails (timeout, HTTP error), the router retries the request on the fallback provider. Configure with `LLM_FALLBACK_*` environment variables.

### Weather
`integrations/weather.py` is a factory that returns either an NWS or Open-Meteo client based on the `WEATHER_PROVIDER` config (`nws` or `open_meteo`). Both expose `current()` and `forecast_today()` async methods.

- **NWS** (`integrations/weather_nws.py`): free National Weather Service API, US-only, no API key. Caches current conditions for 5 minutes, forecast for 10 minutes. Falls back to Open-Meteo for sunrise/sunset.
- **Open-Meteo** (`integrations/weather_open_meteo.py`): free worldwide weather, no API key.

### Sonos
Sonos output is handled by the dedicated `sonos-gateway` service:
- subscribes to `homeagent/announce/request`
- normalizes text for TTS via a fast LLM pass (expands abbreviations like mph → miles per hour, spells out numbers, fixes punctuation)
- generates TTS audio (ElevenLabs)
- hosts the audio over HTTP on the LAN
- plays it on Sonos (SoCo), batching successive announcements before restoring the previous state
- supports mute/unmute via `announce.mute` events on `homeagent/announce/mute`
- publishes `sonos.playback_start` and `sonos.playback_done` on `homeagent/sonos/playback` for voice-service coordination

Announcements can optionally include `data.targets` (aliases or IPs) to direct
playback to specific speakers.

The common "true speech on Sonos" pipeline is:
1) call a TTS API to generate audio bytes
2) host audio on a local HTTP endpoint
3) have Sonos play the audio URL
4) if more announcements are queued, play them without restoring in between
5) restore the previous queue/state after the batch is done

During voice sessions, the gateway enters **hold mode** (via `sonos.hold` events from the voice service) to skip the snapshot restore between the prompt and response — keeping the speaker ready for back-to-back announcements.

Quiet hours and mute are **hard-enforced** in `sonos-gateway`.

### Camect
Camera integration is handled by the `camect-agent` service:
- connects to a Camect hub over websocket
- publishes `camera.event` to MQTT for each matched detection
- optionally enriches announcements with a vision LLM (before/after image comparison, temperature 0.2 for consistency)
- identifies vehicle color/make/model, delivery carriers, and person descriptions
- supports a separate vision endpoint (`CAMECT_VISION_BASE_URL`) or falls back to the main LLM
- **multi-hub support**: run additional instances with `--instance 2` reading from `CAMECT2_*` env vars
- each hub gets a label (e.g., "Lynchburg", "Costa Rica") prepended to announcements

### Temp Stick
`integrations/tempstick.py` fetches sensor data via Temp Stick API (temperature + humidity). The house check agent supports multiple sensors with per-sensor thresholds via `TEMPSTICK_EXTRA_SENSORS` (e.g., remote locations with different climate ranges). Alerts are skipped for offline sensors to avoid false positives on stale data.

### UPS (SNMP)
`integrations/ups_snmp.py` reads UPS input voltage/frequency via SNMP (UPS-MIB by default).

### Internet egress
`integrations/internet_check.py` runs a ping sample to estimate latency + packet loss.

### Remote site check
`hourly_house_check_agent.py` includes a TCP-based reachability check for remote sites (e.g., across a VPN). Uses TCP connect to port 443 instead of ICMP for Docker compatibility.

### API retry
All HTTP-based integrations use a shared `api_retry` decorator (`integrations/_retry.py`): 3 attempts with exponential backoff on timeout and network errors. Covers TTS, STT, weather, TempStick, SimpleFIN, calendar, news feeds, and dashboard scrape.

### SimpleFIN
`integrations/simplefin.py` fetches account balances via SimpleFIN API (read-only financial data).

### Voice STT
The voice service uses Groq's Whisper API (`whisper-large-v3`) for speech-to-text. Before transcription, captured audio goes through a processing pipeline: noise reduction (spectral gating via `noisereduce`), leading/trailing silence trimming, and peak normalization to ~80% of int16 range. This dramatically improves STT accuracy for rooms with background noise or distant microphones. Audio is then converted from raw PCM to WAV before upload. Known Whisper hallucination phrases (e.g. "thank you", "thanks for watching") are filtered out.
