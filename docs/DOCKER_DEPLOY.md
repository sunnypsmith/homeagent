# Docker deployment

This repo can be deployed as a small stack of containers:
- **Mosquitto** (MQTT broker)
- **TimescaleDB/Postgres**
- **Home Agent services** (one container per process)

## Why `network_mode: host` (Sonos)

The Sonos announcement pipeline generates an audio clip and serves it from a tiny local HTTP server using a **random free port**. Sonos speakers must be able to fetch that URL from your network.

On Linux, the simplest reliable setup is running the stack with **host networking**, so the Sonos speakers can reach the audio host without extra port mappings.

## Prereqs

- Docker + Docker Compose (or Portainer stacks)
- A `.env` file in the repo root (never commit it)

## 1) Create `.env`

Copy the template and edit values:

```bash
cp .env.example .env
```

Key fields:
- `MQTT_HOST`, `DB_HOST` (with host networking, `127.0.0.1` is fine)
- `SONOS_SPEAKER_MAP`, `SONOS_GLOBAL_ANNOUNCE_TARGETS`, `ELEVENLABS_API_KEY`
- `SONOS_TAIL_PADDING_SECONDS` (optional; helps prevent clipped endings)
- quiet hours: `QUIET_HOURS_*`
- calendar (optional): `GCAL_*`
- Temp Stick (optional): `TEMPSTICK_*`
- UPS (optional): `UPS_*`
- Internet check (optional): `INTERNET_*`
- SimpleFIN (optional): `SIMPLEFIN_*`
- Executive briefing (optional): `EXEC_BRIEFING_*`
- Camect (optional): `CAMECT_*`
- Caséta + camera lighting (optional): `CASETA_*`, `CAMERA_LIGHTING_*`
  - If you run `caseta-agent` via Compose, also set `CASETA_CERTS_DIR` (host path) so the certs can be mounted into the container at `/certs`.
- Web UI (optional): `UI_*` (bind to your LAN IP, e.g. `UI_BIND_HOST=10.1.1.111`, `UI_PORT=8001`)

## 2) Start the stack

From repo root:

```bash
docker compose -f deploy/docker-compose.yml up -d --build
```

Notes:
- `deploy/docker-compose.yml` is written to pick up values from your **repo-root** `.env` (when you run the command from repo root).
- The TimescaleDB container uses `${DB_NAME}`, `${DB_USER}`, `${DB_PASSWORD}` from `.env` for its initial bootstrap.
- If you enable Caséta, update `CASETA_CERTS_DIR` in your shell environment (or `.env`) to a host directory containing the `lap-pair` generated certs.

Check status:

```bash
docker compose -f deploy/docker-compose.yml ps
```

## 3) Apply DB migrations (one-time)

```bash
docker exec -i home-db psql -U homeagent -d homeagent < db/migrations/0001_timescaledb.sql
docker exec -i home-db psql -U homeagent -d homeagent < db/migrations/0002_events.sql
docker exec -i home-db psql -U homeagent -d homeagent < db/migrations/0003_schedules.sql
```

## 4) Seed schedules

```bash
docker exec -it home-time-trigger home-agent seed-schedules
```

## Logs

```bash
docker compose -f deploy/docker-compose.yml logs -f --tail=100
```

Or per-service:

```bash
docker compose -f deploy/docker-compose.yml logs -f --tail=100 sonos-gateway
```

