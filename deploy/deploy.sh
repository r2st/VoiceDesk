#!/usr/bin/env bash
# Build, migrate and (re)start the VoiceDesk stack. Run from /opt/voicedesk
# on the VPS for every release, including the first:
#
#   cd /opt/voicedesk && git pull && ./deploy/deploy.sh
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DEPLOY_DIR"

if [ ! -f .env ]; then
  echo "deploy/.env is missing. Copy .env.production.example, fill it in, then re-run." >&2
  exit 1
fi

COMPOSE="docker compose -f docker-compose.prod.yml --env-file .env"

echo "==> Building images"
$COMPOSE build

echo "==> Starting infrastructure (postgres, redis, minio) and waiting for health"
$COMPOSE up -d postgres redis minio
for i in $(seq 1 30); do
  unhealthy=$($COMPOSE ps --format '{{.Service}} {{.Health}}' | awk '$2 != "healthy" {print $1}')
  [ -z "$unhealthy" ] && break
  sleep 2
  if [ "$i" -eq 30 ]; then
    echo "Infrastructure did not become healthy in time: $unhealthy" >&2
    exit 1
  fi
done

# The api entrypoint runs `alembic upgrade head` before uvicorn starts, so a
# migration failure fails the container instead of serving traffic against a
# schema the code doesn't expect.
echo "==> Starting the application (api, worker, dashboard, caddy)"
$COMPOSE up -d --remove-orphans

echo "==> Waiting for the API to report healthy"
for i in $(seq 1 30); do
  if $COMPOSE exec -T api python -c \
    "import urllib.request as u; u.urlopen('http://localhost:3010/health/ready', timeout=3)" \
    >/dev/null 2>&1; then
    echo "API is up."
    break
  fi
  sleep 2
  if [ "$i" -eq 30 ]; then
    echo "API did not become healthy. Recent logs:" >&2
    $COMPOSE logs --tail=100 api
    exit 1
  fi
done

echo "==> Pruning unused images to keep the VPS disk from filling up"
docker image prune -f >/dev/null

$COMPOSE ps
echo "==> Deploy complete."
