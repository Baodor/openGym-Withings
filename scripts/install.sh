#!/usr/bin/env bash
# Auf dem Server ausführen, nach git clone. Kein chown -R und keine anderen Container ändern.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
if [[ ! -f .env ]]; then cp .env.example .env; fi
# Do not create an empty host directory: it may hide an unmounted disk.
DATA_PATH="$(python3 - <<'PY'
from pathlib import Path
from scripts.storage_admin import read_env
print(read_env(Path(".env")).get("BRIDGE_DATA_HOST_PATH") or "/mnt/docker/appdata/opengym-withings")
PY
)"
if [[ ! -d "$DATA_PATH" ]]; then
  echo "ABBRUCH: Persistentes Appdata-Verzeichnis fehlt: $DATA_PATH"
  echo "Nur bei einer bewusst neuen Installation: python3 scripts/storage_admin.py init"
  exit 1
fi
python3 scripts/storage_admin.py verify --data "$DATA_PATH" --env .env
python3 scripts/storage_admin.py backup --data "$DATA_PATH" --env .env

if ! docker network inspect proxy >/dev/null 2>&1; then echo 'Docker-Netzwerk proxy fehlt'; exit 1; fi
if ! docker network inspect opengym_backend >/dev/null 2>&1; then echo 'Docker-Netzwerk opengym_backend fehlt'; exit 1; fi
printf '\nPrüfe Compose ...\n'
docker compose config -q
printf 'Baue und starte Bridge ...\n'
docker compose up -d --build
printf '\nSpeicherdaten bleiben persistent. Initialen Setup-Code nur bei einer *neuen* Einrichtung lokal auslesen.\n'
printf '\nWICHTIG: In Pi-hole internen DNS A-Record auf 192.168.1.23 setzen.\n'
printf 'https://opengym-withings.home.glockb.de\n'
