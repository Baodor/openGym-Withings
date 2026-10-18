#!/usr/bin/env bash
# Safe update only: verify persistent store, save consistent backup, then rebuild.
set -Eeuo pipefail
cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo "ABBRUCH: .env fehlt. Kein automatisches Neuanlegen!" >&2
  exit 1
fi

DATA_PATH="$(python3 - <<'PY'
from pathlib import Path
from scripts.storage_admin import read_env
print(read_env(Path(".env")).get("BRIDGE_DATA_HOST_PATH") or "/mnt/docker/appdata/opengym-withings")
PY
)"

echo "=== PERSISTENTEN SPEICHER ÜBERPRÜFEN ==="
python3 scripts/storage_admin.py verify --data "$DATA_PATH" --env .env

echo "=== KONSISTENTES BACKUP ANLEGEN ==="
python3 scripts/storage_admin.py backup --data "$DATA_PATH" --env .env

echo "=== GIT UPDATE (NUR FAST-FORWARD) ==="
git pull --ff-only origin feat/withings-bridge-mvp

echo "=== DOCKER COMPOSE PRÜFEN ==="
docker compose config -q

echo "=== NUR BRIDGE AKTUALISIEREN ==="
docker compose up -d --build --no-deps --force-recreate bridge

echo "=== STATUS ==="
docker compose ps bridge
