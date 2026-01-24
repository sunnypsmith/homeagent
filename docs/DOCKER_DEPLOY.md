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
- `SONOS_ANNOUNCE_TARGETS`, `ELEVENLABS_API_KEY`
- quiet hours: `QUIET_HOURS_*`
- Camect (optional): `CAMECT_*`

## 2) Start the stack

From repo root:

```bash
docker compose -f deploy/docker-compose.yml up -d --build
```

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

