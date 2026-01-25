# Home Agent

Event-driven home automation/agent stack in Python. Services communicate over MQTT and store events/schedules in Postgres/TimescaleDB.

## Quick start (local dev)

```bash
python -m pip install -e .
home-agent --help
```

## Services

- `home-agent sonos-gateway`: MQTT `announce.request` -> TTS -> play on Sonos
- `home-agent time-trigger`: DB schedules -> MQTT time events
- `home-agent event-recorder`: MQTT events -> TimescaleDB
- `home-agent wakeup-agent`: time event -> announce.request
- `home-agent morning-briefing-agent`: time event -> weather + LLM -> announce.request
- `home-agent hourly-chime-agent`: time event -> announce.request
- `home-agent fixed-announcement-agent`: time event -> announce.request

## Docs

- `docs/ARCHITECTURE.md`
- `docs/SONOS_SETUP.md`
- `docs/DB_SETUP.md`
- `docs/SCHEDULING.md`

