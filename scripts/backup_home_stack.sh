#!/usr/bin/env bash
set -euo pipefail

# Back up the complete home-agent stack:
# - Timescale/Postgres (logical dumps; safe while running)
# - Eclipse Mosquitto (tar of /mosquitto/*)
# - home-agent container (docker commit snapshot)
# - .env, esphome configs, models, caseta certs
#
# Usage:
#   ./scripts/backup_home_stack.sh
#
# Optional env overrides:
#   BACKUP_DIR_BASE=/Volumes/seaside/backups/homeagent
#   DB_CONTAINER=home-db
#   DB_USER=homeagent
#   DB_NAME=homeagent
#   MQTT_CONTAINER=home-mqtt
#   AGENT_CONTAINER=homeAgent
#   AGENT_IMAGE=home-agent:py312
#   WORKSPACE=/Volumes/seaside/workspaces/homeAgent
#   SNAPSHOT_CONTAINER=true

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
AGENT_IMAGE="${AGENT_IMAGE:-home-agent:with-init}"
SNAPSHOT_CONTAINER="${SNAPSHOT_CONTAINER:-true}"
WORKSPACE="${WORKSPACE:-$(cd "$(dirname "$0")/.." && pwd)}"

TS="$(date +%F_%H%M%S)"
OUTDIR="${BACKUP_DIR_BASE%/}/${TS}"
mkdir -p "$OUTDIR"

echo "============================================"
echo "Home Agent Backup — $(date)"
echo "Writing to: $OUTDIR"
echo "============================================"

# --- Container metadata ---
echo ""
echo "[1/7] Saving container metadata..."
for c in "$DB_CONTAINER" "$MQTT_CONTAINER" "$AGENT_CONTAINER"; do
  docker inspect "$c" > "$OUTDIR/${c}.inspect.json" 2>/dev/null || echo "  warn: $c not found"
done

# --- Postgres ---
echo "[2/7] Backing up Postgres..."
docker exec -t "$DB_CONTAINER" pg_dumpall -U "$DB_USER" --globals-only 2>/dev/null | gzip -c > "$OUTDIR/db.globals.sql.gz"
docker exec -t "$DB_CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc 2>/dev/null | gzip -c > "$OUTDIR/db.${DB_NAME}.dump.gz"
echo "  db.globals.sql.gz + db.${DB_NAME}.dump.gz"

# --- Mosquitto ---
echo "[3/7] Backing up Mosquitto..."
docker exec -t "$MQTT_CONTAINER" sh -lc '
  set -e
  tar -czf - /mosquitto/config /mosquitto/data /mosquitto/log
' > "$OUTDIR/mosquitto.tgz" 2>/dev/null || echo "  warn: mosquitto backup failed"

# --- Config files ---
echo "[4/7] Backing up config files..."
cp "$WORKSPACE/.env" "$OUTDIR/dot-env.bak" 2>/dev/null || echo "  warn: .env not found"
cp "$WORKSPACE/.env.example" "$OUTDIR/dot-env-example.bak" 2>/dev/null || true
tar -czf "$OUTDIR/esphome.tgz" -C "$WORKSPACE" esphome/ 2>/dev/null || echo "  warn: esphome/ not found"
tar -czf "$OUTDIR/models.tgz" -C "$WORKSPACE" models/ 2>/dev/null || echo "  warn: models/ not found"
tar -czf "$OUTDIR/deploy.tgz" -C "$WORKSPACE" deploy/ 2>/dev/null || echo "  warn: deploy/ not found"
tar -czf "$OUTDIR/scripts.tgz" -C "$WORKSPACE" scripts/ 2>/dev/null || echo "  warn: scripts/ not found"

# Caseta certs (if configured)
if [[ -n "${CASETA_CERTS_DIR:-}" ]] && [[ -d "$CASETA_CERTS_DIR" ]]; then
  tar -czf "$OUTDIR/caseta-certs.tgz" -C "$(dirname "$CASETA_CERTS_DIR")" "$(basename "$CASETA_CERTS_DIR")" 2>/dev/null
  echo "  caseta-certs.tgz"
fi

# --- Container snapshot ---
if [[ "${SNAPSHOT_CONTAINER}" == "true" ]]; then
  echo "[5/7] Creating container snapshot..."
  SNAP_TAG="${AGENT_IMAGE//[:\/]/_}-snapshot-${TS}"
  docker commit "$AGENT_CONTAINER" "$SNAP_TAG" >/dev/null
  docker image save "$SNAP_TAG" | gzip -c > "$OUTDIR/agent-snapshot.image.tar.gz"
  echo "  agent-snapshot.image.tar.gz"
  # Clean up the snapshot tag
  docker image rm "$SNAP_TAG" >/dev/null 2>&1 || true
else
  echo "[5/7] Skipping container snapshot (SNAPSHOT_CONTAINER=false)"
fi

# --- Base image ---
echo "[6/7] Saving base image: ${AGENT_IMAGE}..."
docker image save "$AGENT_IMAGE" 2>/dev/null | gzip -c > "$OUTDIR/home-agent.image.tar.gz" || echo "  warn: image save failed"

# --- Checksums ---
echo "[7/7] Writing checksums..."
if command -v sha256sum >/dev/null 2>&1; then
  (cd "$OUTDIR" && sha256sum ./* > SHA256SUMS.txt 2>/dev/null)
elif command -v shasum >/dev/null 2>&1; then
  (cd "$OUTDIR" && shasum -a 256 ./* > SHA256SUMS.txt 2>/dev/null)
fi

# --- Summary ---
echo ""
echo "============================================"
echo "Backup complete: $OUTDIR"
echo "Contents:"
ls -lh "$OUTDIR/"
echo ""
echo "Total size: $(du -sh "$OUTDIR" | cut -f1)"
echo "============================================"

cat <<'EOF'

Restore notes:
  Postgres globals:
    gunzip -c db.globals.sql.gz | docker exec -i home-db psql -U postgres
  Postgres DB:
    gunzip -c db.homeagent.dump.gz | docker exec -i home-db pg_restore -U homeagent -d homeagent --clean --if-exists
  Mosquitto:
    cat mosquitto.tgz | docker exec -i home-mqtt sh -lc 'tar -xzf - -C /'
  Container image:
    gunzip -c agent-snapshot.image.tar.gz | docker image load
  Config:
    cp dot-env.bak /path/to/workspace/.env
    tar -xzf esphome.tgz -C /path/to/workspace/
    tar -xzf models.tgz -C /path/to/workspace/
EOF
