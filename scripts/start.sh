#!/usr/bin/env sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
command -v docker >/dev/null 2>&1 || { echo 'Docker CLI is unavailable.' >&2; exit 1; }
docker info --format '{{.ServerVersion}}'
if [ ! -f .env ]; then
    python3 scripts/init_env.py
fi
docker compose up -d --build --wait --wait-timeout 240
printf '%s\n' 'Deployment is healthy. Default address: http://127.0.0.1:28671 (or your .env WEB_PORT).'
