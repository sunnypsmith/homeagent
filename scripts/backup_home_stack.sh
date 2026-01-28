#!/usr/bin/env bash
set -euo pipefail

# Back up the core "home agent" docker stack:
# - Timescale/Postgres (logical dumps; safe while running)
# - Eclipse Mosquitto (tar of /mosquitto/*)
# - home-agent image (docker save)
#
# Usage:
#   ./scripts/backup_home_stack.sh
#
# Optional env overrides:
#   BACKUP_DIR_BASE=backups
#   DB_CONTAINER=home-db
#   DB_USER=homeagent
#   DB_NAME=homeagent
#   MQTT_CONTAINER=mqtt
#   AGENT_CONTAINER=homeAgent
#   AGENT_IMAGE=home-agent:py312
#   SNAPSHOT_CONTAINER=true   # also docker commit the running AGENT_CONTAINER

need() {
  command -v "$1" >/dev/null 2>&1 || { echo "Missing required command: $1" >&2; exit 1; }
}

need docker
need date
need gzip

BACKUP_DIR_BASE="${BACKUP_DIR_BASE:-backups}"
DB_CONTAINER="${DB_CONTAINER:-home-db}"
DB_USER="${DB_USER:-homeagent}"
DB_NAME="${DB_NAME:-homeagent}"
MQTT_CONTAINER="${MQTT_CONTAINER:-mqtt}"
AGENT_CONTAINER="${AGENT_CONTAINER:-homeAgent}"
AGENT_IMAGE="${AGENT_IMAGE:-home-agent:py312}"
SNAPSHOT_CONTAINER="${SNAPSHOT_CONTAINER:-false}"

TS="$(date +%F_%H%M%S)"
OUTDIR="${BACKUP_DIR_BASE%/}/${TS}"
mkdir -p "$OUTDIR"

echo "Writing backups to: $OUTDIR"

echo "Saving docker inspect metadata..."
docker inspect "$DB_CONTAINER" > "$OUTDIR/${DB_CONTAINER}.inspect.json"
docker inspect "$MQTT_CONTAINER" > "$OUTDIR/${MQTT_CONTAINER}.inspect.json"
docker inspect "$AGENT_CONTAINER" > "$OUTDIR/${AGENT_CONTAINER}.inspect.json" || true

echo "Backing up Postgres (logical dumps, online)..."
docker exec -t "$DB_CONTAINER" pg_dumpall -U "$DB_USER" --globals-only | gzip -c > "$OUTDIR/${DB_CONTAINER}.globals.sql.gz"
docker exec -t "$DB_CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc | gzip -c > "$OUTDIR/${DB_CONTAINER}.${DB_NAME}.dump.gz"

echo "Backing up Mosquitto (/mosquitto/config, /mosquitto/data, /mosquitto/log)..."
docker exec -t "$MQTT_CONTAINER" sh -lc '
  set -e
  tar -czf - /mosquitto/config /mosquitto/data /mosquitto/log
' > "$OUTDIR/${MQTT_CONTAINER}.mosquitto.tgz"

if [[ "${SNAPSHOT_CONTAINER}" == "true" ]]; then
  SNAP_TAG="${AGENT_IMAGE//[:\/]/_}-snapshot-${TS}"
  echo "Creating container snapshot image: ${SNAP_TAG}"
  docker commit "$AGENT_CONTAINER" "$SNAP_TAG" >/dev/null
  echo "Saving snapshot image..."
  docker image save "$SNAP_TAG" | gzip -c > "$OUTDIR/${AGENT_CONTAINER}.snapshot.image.tar.gz"
fi

echo "Saving home-agent image: ${AGENT_IMAGE}"
docker image save "$AGENT_IMAGE" | gzip -c > "$OUTDIR/home-agent.image.tar.gz"

if command -v sha256sum >/dev/null 2>&1; then
  echo "Writing checksums..."
  (cd "$OUTDIR" && sha256sum ./* > SHA256SUMS.txt)
fi

cat <<'EOF'

Restore notes (high-level):
  - Postgres globals:
      gunzip -c home-db.globals.sql.gz | docker exec -i home-db psql -U postgres
  - Postgres DB (custom dump):
      gunzip -c home-db.homeagent.dump.gz | docker exec -i home-db pg_restore -U homeagent -d homeagent --clean --if-exists
  - Mosquitto:
      cat mqtt.mosquitto.tgz | docker exec -i mqtt sh -lc 'tar -xzf - -C /'
  - Image restore:
      gunzip -c home-agent.image.tar.gz | docker image load
EOF

echo "Done."

