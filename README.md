# Home Agent

Event-driven home automation / “house agent” stack in Python.

- Services communicate over **MQTT** (message bus)
- Events + schedules are stored in **Postgres/TimescaleDB**
- Sonos output is handled by a dedicated **gateway** service (hard-enforces quiet hours)

## Features (current)

- **Always-on service stack**: one process per responsibility (gateway, recorder, schedulers, agents)
- **Sonos announcements**: MQTT `announce.request` → TTS (ElevenLabs) → host audio → Sonos playback (SoCo)
- **Quiet hours**: hard-enforced in `sonos-gateway` (prevents accidental night announcements)
- **Scheduling in DB**: cron schedules in Postgres, published as time events over MQTT
- **Agents**:
  - wakeup call (weather + time)
  - morning briefing (LLM + weather + optional calendar ICS)
  - hourly chime
  - fixed announcements (arbitrary text on a schedule)
- **Camect integration (optional)**: consume AI alerts, publish `camera.event`, optionally announce with vision-enriched descriptions
- **Lutron Caséta integration (optional)**: control devices + scenes (virtual buttons) via LEAP
- **Camera → lighting automation (optional)**: turn on/off selected Caséta devices based on Camect events + “after dark”
- **Sunset scene (optional)**: trigger a Caséta scene at local sunset
- **Home checks (optional)**: scheduled health checks (e.g., Temp Stick thresholds)
- **Executive briefing (optional)**: M-F briefing with weather, calendar, financial summary (SimpleFIN), dashboard metrics, and configurable news feeds
- **Voice assistant (optional)**: M5Stack Atom Echo devices stream UDP audio to a voice service (Picovoice Porcupine or Whisper-based wake word, WebRTC VAD, audio processing pipeline with noise reduction + normalization + silence trimming, Groq Whisper STT) which publishes commands to a voice-intent agent (LLM tool-calling for actions + Sonos spoken responses)
- **NWS weather provider (optional)**: National Weather Service API as an alternative to Open-Meteo (no API key, US-only, built-in response caching)
- **LLM fallback**: automatic failover to a secondary LLM provider when the primary is down (`LLM_FALLBACK_*` config)
- **Status dashboard**: real-time `/status` page on the UI gateway showing service health, MQTT activity, DB events, voice room status, recent commands, and errors

## Quick start (local dev)

```bash
python -m pip install -e .
home-agent --help
```

Optional feature extras (install what you use):

```bash
pip install -e ".[sonos]"   # Sonos discovery + playback
pip install -e ".[gcal]"    # Calendar ICS parsing (Google/iCloud)
pip install -e ".[camect]"  # Camect hub integration
pip install -e ".[caseta]"  # Lutron Caséta integration (+ CLI tools)
pip install -e ".[ui]"      # Simple LAN web UI (buttons -> MQTT announce.request)
pip install -e ".[snmp]"    # UPS monitoring via SNMP
pip install -e ".[net]"     # Internet egress check (ping)
pip install -e ".[voice]"  # Voice assistant (Porcupine, WebRTC VAD, noisereduce)
pip install -e ".[llm-anthropic]"  # Anthropic Claude reasoning LLM
```

Install everything at once:

```bash
pip install -e ".[sonos,gcal,camect,caseta,ui,snmp,net,voice,llm-anthropic]"
```

## Quick start (Docker / recommended on Linux)

This is the easiest way to run the full stack long-term, especially with Sonos (host networking).

```bash
cp .env.example .env
# edit .env (never commit it)

docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml ps
```

One-time DB migrations:

```bash
docker exec -i home-db psql -U homeagent -d homeagent < db/migrations/0001_timescaledb.sql
docker exec -i home-db psql -U homeagent -d homeagent < db/migrations/0002_events.sql
docker exec -i home-db psql -U homeagent -d homeagent < db/migrations/0003_schedules.sql
docker exec -i home-db psql -U homeagent -d homeagent < db/migrations/0004_events_ingested_idx.sql
```

Seed default schedules:

```bash
docker exec -it home-time-trigger home-agent seed-schedules
```

## Services

- `home-agent sonos-gateway`: MQTT `announce.request` -> TTS -> play on Sonos
- `home-agent time-trigger`: DB schedules -> MQTT time events
- `home-agent event-recorder`: MQTT events -> TimescaleDB
- `home-agent ui-gateway`: LAN web UI — controls at `/`, real-time status dashboard at `/status`
- `home-agent wakeup-agent`: time event -> announce.request
- `home-agent morning-briefing-agent`: time event -> weather + LLM (+ optional calendar ICS) -> announce.request
- `home-agent hourly-chime-agent`: time event -> announce.request
- `home-agent fixed-announcement-agent`: time event -> announce.request
- `home-agent camect-agent`: Camect hub -> MQTT camera events (+ optional announcements). Supports `--instance 2` for multi-hub setups.
- `home-agent caseta-agent`: Lutron Caséta bridge -> MQTT commands/events
- `home-agent camera-lighting-agent`: camera events -> Caséta lighting automation
- `home-agent hourly-house-check-agent`: scheduled checks (e.g., Temp Stick thresholds)
- `home-agent exec-briefing-agent`: daily executive briefing (weather + calendar + financial)
- `home-agent watchdog`: monitors all services via heartbeats/errors, announces failures, restarts crashed processes
- `home-agent voice-service`: UDP audio receiver (Atom Echo) → Porcupine → VAD → noise reduction + normalization → Groq Whisper STT → MQTT `voice.command`
- `home-agent voice-intent-agent`: voice commands → LLM tool-calling → actions + Sonos spoken responses
- `home-agent monitor`: terminal dashboard (rich TUI) for MQTT activity and service status

## Common examples

### Global SMTP (optional)

Used by modules that send email (e.g. Camect snapshot-to-email).

```bash
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=you@example.com
SMTP_PASSWORD=APP_PASSWORD_OR_SMTP_PASSWORD
SMTP_FROM=Home Agent <you@example.com>
SMTP_USE_STARTTLS=true
SMTP_USE_SSL=false
SMTP_TIMEOUT_SECONDS=20
```

### Temp Stick thresholds (optional)

```bash
TEMPSTICK_ENABLED=true
TEMPSTICK_API_KEY=YOUR_TEMPSTICK_KEY_HERE
TEMPSTICK_SENSOR_NAME=Greatroom
TEMPSTICK_TEMP_LOW_F=60
TEMPSTICK_TEMP_HIGH_F=78
TEMPSTICK_HUMIDITY_LOW=20
TEMPSTICK_HUMIDITY_HIGH=60
```

Monitor additional sensors (e.g., remote locations) with per-sensor thresholds:

```bash
# Format: name:temp_high_f:humidity_high;name:temp_high_f:humidity_high
TEMPSTICK_EXTRA_SENSORS=CR Upstairs:90:55;CR-Downstairs:90:55
```

Set these in your repo-root `.env`.

### Remote site check (optional)

TCP reachability check for a remote site (e.g., across a VPN):

```bash
REMOTE_SITE_CHECK_ENABLED=true
REMOTE_SITE_CHECK_HOST=10.1.4.254
REMOTE_SITE_CHECK_LABEL=Costa Rica
REMOTE_SITE_CHECK_PING_COUNT=10
REMOTE_SITE_CHECK_TIMEOUT_SECONDS=15
```

### UPS line input thresholds (optional)

```bash
UPS_ENABLED=true
UPS_HOST=10.1.2.200
UPS_COMMUNITY=public
UPS_INPUT_VOLTAGE_LOW=108
UPS_INPUT_VOLTAGE_HIGH=126
UPS_INPUT_FREQUENCY_LOW=59.5
UPS_INPUT_FREQUENCY_HIGH=60.5
```

Set these in your repo-root `.env`.

### Internet egress check (optional)

```bash
INTERNET_CHECK_ENABLED=true
INTERNET_CHECK_HOST=1.1.1.1
INTERNET_CHECK_DURATION_SECONDS=10
INTERNET_MAX_PACKET_LOSS_PERCENT=1
INTERNET_MAX_LATENCY_MS=100
```

Set these in your repo-root `.env`.

### Executive briefing (optional)

```bash
SIMPLEFIN_ENABLED=true
SIMPLEFIN_ACCESS_URL=https://user:pass@beta-bridge.simplefin.org/simplefin
EXEC_BRIEFING_TARGETS=office
EXEC_BRIEFING_ICS_URL=https://calendar.google.com/calendar/ical/.../basic.ics
EXEC_BRIEFING_DASHBOARD_URL=http://your-dashboard:port/path
EXEC_BRIEFING_NEWS_HEADLINES=5
EXEC_BRIEFING_FEED_1=AI News|https://rss.app/feeds/v1.1/XXXX.json
EXEC_BRIEFING_FEED_2=Tech|https://rss.app/feeds/v1.1/YYYY.json
```

Trigger manually:

```bash
home-agent trigger-exec-briefing
```

### Camect vision analysis (optional)

Enrich camera announcements with vision LLM descriptions. Uses before/after image comparison (60s apart) to identify what's new — vehicle color/make/model, delivery carrier, person description:

```bash
CAMECT_VISION_ENABLED=true
CAMECT_VISION_MODEL=meta-llama/llama-4-maverick-17b-128e-instruct
CAMECT_VISION_TIMEOUT_SECONDS=10

# Optional: use a different endpoint/key for vision (falls back to LLM_BASE_URL / LLM_API_KEY)
CAMECT_VISION_BASE_URL=https://api.openai.com/v1
CAMECT_VISION_API_KEY=sk-...
CAMECT_VISION_DETAIL=auto   # low | high | auto
```

### Watchdog (recommended)

Monitors all services via MQTT heartbeats and error events. On failure: logs to DB, announces the error on Sonos, and attempts a single restart.

```bash
home-agent watchdog
```

To enable automatic restarts, set a tmux pane mapping so the watchdog knows where each service runs:

```bash
WATCHDOG_TMUX_MAP=sonos-gateway:homeagent:0.1,camect-agent:homeagent:2.0,time-trigger:homeagent:0.0
```

### Voice assistant (optional)

Hands-free voice control via M5Stack Atom Echo devices running custom ESPHome firmware.
The Atom Echos stream 16 kHz PCM audio over UDP to the voice service, which runs
Picovoice Porcupine (when `VOICE_WAKE_ENGINE=porcupine`) or Whisper-based wake detection, WebRTC VAD, and an audio processing pipeline
(noise reduction, silence trimming, peak normalization) before Groq Whisper STT.
Transcribed commands are published to MQTT and processed by the voice-intent agent
(LLM with tool calling). Responses are normalized for TTS (abbreviation expansion,
number spelling) and spoken back on the nearest Sonos speaker.

```bash
# Voice service config
VOICE_UDP_PORT=9100
VOICE_ROOMS=office=offi,kitchen=ktch
VOICE_ROOM_SPEAKERS=offi:office,ktch:kitchen_dining

# Wake word: porcupine (Picovoice) or whisper (STT chunks; default whisper)
VOICE_WAKE_ENGINE=porcupine
VOICE_PORCUPINE_KEY=YOUR_PICOVOICE_KEY
VOICE_PORCUPINE_MODEL=models/Master-Higgins_en_linux_v4_0_0.ppn
VOICE_WAKE_COOLDOWN=2.0

# STT
VOICE_STT_PROVIDER=groq
VOICE_STT_API_KEY=YOUR_GROQ_KEY_HERE
VOICE_STT_MODEL=whisper-large-v3
VOICE_STT_LANGUAGE=en
```

Run the two services:

```bash
home-agent voice-service
home-agent voice-intent-agent
```

ESPHome device configs live in `esphome/`. Flash with `esphome run esphome/atom-echo-voice.yaml`.

The voice service enters a "deaf" state when Sonos is playing (`sonos.playback_start` / `sonos.playback_done` MQTT events) to avoid picking up its own announcements.

### NWS weather provider (optional)

Switch from Open-Meteo to the National Weather Service API (free, no key, US locations only):

```bash
WEATHER_PROVIDER=nws
```

Built-in response caching (5 min current conditions, 10 min forecast). Falls back to Open-Meteo for sunrise/sunset data.

### Offline announcement audio (optional)

Generate offline WAV files (uses ElevenLabs settings in `.env`):

```bash
python scripts/generate_offline_audio.py
```

Files are written to `OFFLINE_AUDIO_DIR` (default: `assets/offline`).

### Sonos discovery (writes `SONOS_SPEAKER_MAP`)

```bash
python3 scripts/sonos_discover.py --write
# or (if multicast/SSDP is blocked)
python3 scripts/sonos_discover.py --subnet 192.168.1.0/24 --write
```

This writes `SONOS_SPEAKER_MAP` + `SONOS_GLOBAL_ANNOUNCE_TARGETS` (aliases + default targets).

Optional per-agent targets:
```bash
SONOS_MORNING_BRIEFING_TARGETS=office
SONOS_WAKEUP_TARGETS=bedroom
```

### TTS → Sonos end-to-end test

```bash
home-agent tts-test "Hello from the home agent"
```

### Publish a manual announcement over MQTT

Requires a running `home-agent sonos-gateway` (and your broker running).

```bash
mosquitto_pub -h "$MQTT_HOST" -p "$MQTT_PORT" -t 'homeagent/announce/request' -m '{
  "id":"manual-1",
  "ts":"2026-01-01T00:00:00Z",
  "source":"manual",
  "type":"announce.request",
  "trace_id":"manual-1",
  "data":{"text":"Hello from MQTT"}
}'
```

### Trigger a morning briefing now

```bash
home-agent trigger-morning-briefing
```

### Simple LAN web UI (buttons)

Enable the UI service (example LAN IP):

```bash
UI_ENABLED=true
UI_BIND_HOST=10.1.1.111
UI_PORT=8001
UI_TITLE=Smith Home Agent
UI_ACTION_1=dinner|Call to Dinner|Dinner time. Please come to the table.
UI_ACTION_2=kids_up|Kids Upstairs|Kids, please come upstairs.
```

Run it:

```bash
home-agent ui-gateway
```

Then open on your iPhone:

- `http://10.1.1.111:8001/`

Built-in controls include:
- Mute (1 hour) / Unmute
- Test Tone (10s)

### Fixed announcements (DB-backed schedules)

Add/update:

```bash
home-agent add-fixed-announcement --name kids_bedtime_2000 --at 20:00 --days "*" \
  "It is eight o'clock. Time for kids to take showers and get ready for bed."
```

List:

```bash
home-agent list-fixed-announcements
home-agent list-fixed-announcements --enabled-only
```

### Caséta scenes (virtual buttons) + scheduling

Pairing + cert paths are covered in `docs/CASETA_SETUP.md`.

Schedule “Daytime” scene:

```bash
home-agent add-caseta-scene --name caseta_daytime_weekday_0600 --at 06:00 --days mon-fri --scene-name Daytime
home-agent add-caseta-scene --name caseta_daytime_weekend_0700 --at 07:00 --days sat,sun --scene-name Daytime
```

Sunset scene (runs daily at local sunset via `time-trigger` + Open‑Meteo):

```bash
SUNSET_SCENE_ENABLED=true
SUNSET_SCENE_NAME=Nighttime
SUNSET_SCENE_OFFSET_MINUTES=0
```

### Multi-hub Camect (optional)

Run a second Camect agent instance for a remote hub (e.g., Costa Rica):

```bash
# In .env — add CAMECT2_* variables
CAMECT2_ENABLED=true
CAMECT2_HUB_LABEL=Costa Rica
CAMECT2_HOST=10.1.4.245:443
CAMECT2_USERNAME=admin
CAMECT2_PASSWORD=YOUR_PASSWORD
CAMECT2_CAMERA_RULES="Living Room:person,vehicle;Kitchen:person,vehicle"
CAMECT2_VISION_ENABLED=true
CAMECT2_VISION_MODEL=gpt-4.1-mini
```

Run with `--instance 2`:

```bash
home-agent camect-agent --instance 2
```

Announcements are prefixed with the hub label: "Costa Rica: person detected at Kitchen."

### Camect rules + camera → lighting (optional)

Minimal `.env` snippets:

```bash
# Camect (publish camera events and optionally announce)
CAMECT_ENABLED=true
CAMECT_HOST=10.1.2.150:443
CAMECT_USERNAME=admin
CAMECT_PASSWORD=YOUR_PASSWORD
CAMECT_CAMERA_RULES="Front_Garage:vehicle,car,truck,van,suv;Front_Door:person,people,human"
CAMECT_EMAIL_ALERT_PICS_TO=you@example.com

# Camera lighting (turn on selected Caséta devices for 10 minutes, only when dark)
CAMERA_LIGHTING_ENABLED=true
CAMERA_LIGHTING_ONLY_DARK=true
CAMERA_LIGHTING_CAMERA_NAME=Front_Door,Front_Garage
CAMERA_LIGHTING_DETECTED_OBJ=vehicle,person
CAMERA_LIGHTING_CASETA_DEVICE_ID=7,47
CAMERA_LIGHTING_DURATION_SECONDS=600
```

## Docs

- `docs/ARCHITECTURE.md` — system design and service overview
- `docs/VOICE_ASSISTANT.md` — voice pipeline, ESPHome firmware, configuration, troubleshooting
- `docs/WEATHER.md` — weather provider factory (NWS + Open-Meteo)
- `docs/WATCHDOG.md` — service health monitoring and auto-restart
- `docs/SONOS_SETUP.md` — speaker discovery, volumes, playback events
- `docs/DB_SETUP.md` — TimescaleDB migrations and retention
- `docs/SCHEDULING.md` — cron/interval schedules
- `docs/CALENDAR_SETUP.md` — Google Calendar / iCloud ICS
- `docs/DOCKER_DEPLOY.md` — Docker Compose deployment
- `docs/CAMECT_SETUP.md` — Camect camera integration
- `docs/CASETA_SETUP.md` — Lutron Caséta lighting
- `docs/CAMERA_LIGHTING.md` — camera → lighting automation
- `esphome/` — ESPHome configs for M5Stack Atom Echo voice devices

