# Home Agent (Python)

A modular, professional framework for a **constantly running home AI agent**:

- **Event-driven services**: small processes that communicate over **MQTT**
- **Storage**: record events + store schedules in **TimescaleDB/Postgres**
- **Integrations**: LLM (OpenAI-compatible), ElevenLabs TTS, Sonos playback, weather (Open-Meteo)
- **Operability**: structured logs, `.env` configuration, strict event envelope

## Quick start

### 1) Create venv + install

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .
```

Optional Sonos support:

```bash
pip install -e ".[sonos]"
```

Optional integrations:

```bash
# Cameras (Camect)
pip install -e ".[camect]"

# Lighting (Lutron Caséta)
pip install -e ".[caseta]"
```

### 2) Configure

Copy `.env.example` to `.env` and edit:

```bash
cp .env.example .env
```

### 3) Run

CLI (preferred):

```bash
home-agent --help
```

If `home-agent` isn’t on your PATH (common in containers), use:

```bash
python3 -m home_agent.cli --help
```

## Deploy

- Docker stack: see `docs/DOCKER_DEPLOY.md`

## Run the stack (service-based)

Always-on infrastructure:
- **MQTT broker** (Mosquitto)
- **TimescaleDB/Postgres**

Always-on Home Agent services:
- `home-agent sonos-gateway` (MQTT announce → TTS → Sonos)
- `home-agent time-trigger` (DB schedules → MQTT time events)
- `home-agent event-recorder` (MQTT → DB events table)

Always-on agents (examples):
- `home-agent wakeup-agent`
- `home-agent morning-briefing-agent`
- `home-agent hourly-chime-agent`
- `home-agent fixed-announcement-agent`

Quiet hours:
- enforced in **`home-agent sonos-gateway`** (nothing plays during quiet hours)
- configure via `QUIET_HOURS_*` in `.env`

## Setup checklists (most common)

### Sonos announcements (TTS → speakers)

- Set `SONOS_ANNOUNCE_TARGETS` (see `docs/SONOS_SETUP.md`)
- Set `ELEVENLABS_API_KEY`
- Run:
  - `home-agent sonos-gateway`
- Test:

```bash
home-agent tts-test "Hello from the home agent"
```

### Camect camera events

- Configure `CAMECT_*` in `.env` (see `docs/CAMECT_SETUP.md`)
- Run:
  - `home-agent camect-agent`
- Watch events:

```bash
mosquitto_sub -h "$MQTT_HOST" -p "$MQTT_PORT" -t 'homeagent/camera/event' -v
```

### Lutron Caséta control + device IDs

- Pair once (generates TLS certs):

```bash
lap-pair <BRIDGE_IP>
```

- Configure `CASETA_*` in `.env` (see `docs/CASETA_SETUP.md`)
- Run:
  - `home-agent caseta-agent`
- Find device IDs (from retained snapshot `lutron.devices`):

```bash
mosquitto_sub -h "$MQTT_HOST" -p "$MQTT_PORT" -t 'homeagent/lutron/event' -v
```

### Camera → lighting automation (Camect → Caséta)

- Configure `CAMECT_*` and `CASETA_*`
- Configure `CAMERA_LIGHTING_*` (supports lists for camera names, object tokens, and device IDs)
  - See `docs/CAMERA_LIGHTING.md`
- Run:
  - `home-agent camect-agent`
  - `home-agent caseta-agent`
  - `home-agent camera-lighting-agent`

## Schedules

- Seed core schedules into Postgres:

```bash
home-agent seed-schedules
```

- Add/update a fixed announcement (upsert by `--name`):

```bash
home-agent add-fixed-announcement --name kids_bedtime_2000 --at 20:00 --days "*" "Time for bed."
```

- List fixed announcements:

```bash
home-agent list-fixed-announcements
```

## How to extend

- Add a new module in `src/home_agent/modules/` implementing `Module`
- Register it in `src/home_agent/modules/registry.py`
- The module can:
  - schedule jobs (cron/interval)
  - publish/subscribe events
  - call integrations (LLM, Sonos, HTTP)

## Notes on Sonos “speech”

Sonos typically plays an **audio URL** (or uses services that can play a stream). This framework provides:

- a **stub announcer** (logs what it would say)
- a **SoCo-based announcer** (requires `pip install -e ".[sonos]"`)

### Discover Sonos speakers + set config

Use the setup utility to find speakers and write `SONOS_ANNOUNCE_TARGETS` into your `.env`:

```bash
python3 scripts/sonos_discover.py
python3 scripts/sonos_discover.py --write
```

If SSDP/multicast is blocked, scan by subnet:

```bash
python3 scripts/sonos_discover.py --subnet 192.168.1.0/24 --write
```

More details: see `docs/SONOS_SETUP.md`.

## Integrations setup guides

- Sonos: `docs/SONOS_SETUP.md`
- Camect cameras: `docs/CAMECT_SETUP.md`
- Lutron Caséta: `docs/CASETA_SETUP.md`
- Camera → lighting automation: `docs/CAMERA_LIGHTING.md`

If you want “true TTS”, the usual pattern is:
1) call a TTS API to generate audio, 2) host it (local HTTP), 3) tell Sonos to play the URL.

This repo is structured so you can drop in that TTS+hosting module cleanly.
