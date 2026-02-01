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
- **Camect integration (optional)**: consume AI alerts, publish `camera.event`, and optionally announce
- **Lutron Caséta integration (optional)**: control devices + scenes (virtual buttons) via LEAP
- **Camera → lighting automation (optional)**: turn on/off selected Caséta devices based on Camect events + “after dark”
- **Sunset scene (optional)**: trigger a Caséta scene at local sunset

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
```

Seed default schedules:

```bash
docker exec -it home-time-trigger home-agent seed-schedules
```

## Services

- `home-agent sonos-gateway`: MQTT `announce.request` -> TTS -> play on Sonos
- `home-agent time-trigger`: DB schedules -> MQTT time events
- `home-agent event-recorder`: MQTT events -> TimescaleDB
- `home-agent ui-gateway`: simple LAN web UI (buttons -> MQTT announce.request)
- `home-agent wakeup-agent`: time event -> announce.request
- `home-agent morning-briefing-agent`: time event -> weather + LLM (+ optional calendar ICS) -> announce.request
- `home-agent hourly-chime-agent`: time event -> announce.request
- `home-agent fixed-announcement-agent`: time event -> announce.request
- `home-agent camect-agent`: Camect hub -> MQTT camera events (+ optional announcements)
- `home-agent caseta-agent`: Lutron Caséta bridge -> MQTT commands/events
- `home-agent camera-lighting-agent`: camera events -> Caséta lighting automation

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

### Sonos discovery (writes `SONOS_ANNOUNCE_TARGETS`)

```bash
python3 scripts/sonos_discover.py --write
# or (if multicast/SSDP is blocked)
python3 scripts/sonos_discover.py --subnet 192.168.1.0/24 --write
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

- `docs/ARCHITECTURE.md`
- `docs/SONOS_SETUP.md`
- `docs/DB_SETUP.md`
- `docs/SCHEDULING.md`
- `docs/CALENDAR_SETUP.md`
- `docs/DOCKER_DEPLOY.md`
- `docs/CAMECT_SETUP.md`
- `docs/CASETA_SETUP.md`
- `docs/CAMERA_LIGHTING.md`

