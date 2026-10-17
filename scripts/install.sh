#!/usr/bin/env bash
# Auf dem Server ausführen, nach git clone. Kein chown -R und keine anderen Container ändern.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
if [[ ! -f .env ]]; then cp .env.example .env; fi
mkdir -p /mnt/docker/appdata/opengym-withings
if [[ ! -w /mnt/docker/appdata/opengym-withings ]]; then
  printf 'Keine Schreibrechte auf /mnt/docker/appdata/opengym-withings.\n'
  printf 'Bitte gezielt Rechte/ACL für den ausführenden Benutzer konfigurieren.\n'
  exit 1
fi
if ! docker network inspect proxy >/dev/null 2>&1; then echo 'Docker-Netzwerk proxy fehlt'; exit 1; fi
if ! docker network inspect opengym_backend >/dev/null 2>&1; then echo 'Docker-Netzwerk opengym_backend fehlt'; exit 1; fi
printf '\nPrüfe Compose ...\n'
docker compose config -q
printf 'Baue und starte Bridge ...\n'
docker compose up -d --build
printf '\nInitialer Setup-Code (nur lokal lesen; nicht im Chat senden):\n'
for _ in {1..30}; do
  [[ -f /mnt/docker/appdata/opengym-withings/setup.key ]] && break
  sleep 1
done
if [[ -f /mnt/docker/appdata/opengym-withings/setup.key ]]; then
  cat /mnt/docker/appdata/opengym-withings/setup.key
else
  echo 'Setup-Code noch nicht vorhanden. docker compose logs --tail=80 bridge prüfen.'
fi
printf '\nWICHTIG: In Pi-hole internen DNS A-Record auf 192.168.1.23 setzen.\n'
printf 'https://opengym-withings.home.glockb.de\n'
