# Home Agent

Event-driven home automation/agent stack in Python. Services communicate over MQTT and store events/schedules in Postgres/TimescaleDB.

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
```

## Services

- `home-agent sonos-gateway`: MQTT `announce.request` -> TTS -> play on Sonos
- `home-agent time-trigger`: DB schedules -> MQTT time events
- `home-agent event-recorder`: MQTT events -> TimescaleDB
- `home-agent wakeup-agent`: time event -> announce.request
- `home-agent morning-briefing-agent`: time event -> weather + LLM -> announce.request
- `home-agent hourly-chime-agent`: time event -> announce.request
- `home-agent fixed-announcement-agent`: time event -> announce.request
- `home-agent camect-agent`: Camect hub -> MQTT camera events (+ optional announcements)
- `home-agent caseta-agent`: Lutron Caséta bridge -> MQTT commands/events
- `home-agent camera-lighting-agent`: camera events -> Caséta lighting automation

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

