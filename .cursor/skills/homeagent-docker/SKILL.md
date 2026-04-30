---
name: homeagent-docker
description: >-
  homeAgent Docker build and deploy conventions. Use when modifying the
  Dockerfile, docker-compose.yml, or deploying container changes.
metadata:
  surfaces:
    - ide
---
# homeAgent Docker Conventions

## Dockerfile Layer Caching

The Dockerfile uses a two-stage build with careful layer ordering for fast rebuilds.

**Critical rule**: Dependencies are installed BEFORE source code is copied. This means code-only changes rebuild in ~10 seconds instead of ~5 minutes.

```
Builder stage:
1. COPY pyproject.toml setup.py  →  cached unless deps change
2. pip install dependencies     →  cached unless deps change
3. playwright install           →  cached unless deps change

Runtime stage:
1. Copy site-packages from builder  →  cached
2. COPY src, assets, db, etc.       →  invalidated on any code change
3. pip install --no-deps -e .       →  fast, no downloads
```

**Never** put `COPY src ./src` before `pip install` in the builder stage. That defeats the layer cache.

## Deploying Changes

All services share the same Docker image (defined by the `x-ha-service` anchor in docker-compose.yml). To deploy a code change:

```bash
cd deploy
docker compose build <service-name>    # rebuilds image
docker compose up -d --force-recreate <service-name>  # restarts container
```

The `--force-recreate` flag is required when only the image changed (not the compose config), otherwise Docker reuses the existing container.

Service names match docker-compose.yml: `sonos-gateway`, `voice-service`, `voice-intent-agent`, `camect-agent`, etc.

## .env Configuration

The `.env` file is bind-mounted read-only into all containers at `/workspace/.env`. Changes to `.env` take effect on container restart without rebuilding.

## Database Migrations

Migrations are in `db/migrations/` and run manually:

```bash
docker exec -i home-db psql -U homeagent -d homeagent < db/migrations/NNNN_name.sql
```
