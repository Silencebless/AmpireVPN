#!/usr/bin/env bash
# Обновление AmpireVPN без простоя: реплики api заменяются ПО ОДНОЙ, worker — после них.
#
#   ./deploy/scripts/rolling-update.sh                 # использует docker-compose.scale.yml
#   COMPOSE_FILE_ARGS="-f docker-compose.scale.yml -f docker-compose.backup.yml" ./deploy/scripts/rolling-update.sh
#
# Как это работает:
#   1. собираем новый образ (старые контейнеры продолжают работать);
#   2. поднимаем рядом ту же численность реплик api на новом образе (--no-recreate не трогает старые);
#   3. ждём, пока новые станут healthy; затем каждую старую реплику останавливаем с grace-периодом (она дорабатывает
#      запросы и фоновые задачи), Caddy за несколько секунд перестаёт её выбирать, клиенты ошибок не видят;
#   4. worker перезапускается последним (его задачи идемпотентны и защищены блокировками).
# Миграции БД применяет первая запустившаяся новая реплика; они должны быть обратно совместимы со старым кодом.
set -euo pipefail
cd "$(dirname "$0")/../.."

FILES=${COMPOSE_FILE_ARGS:--f docker-compose.scale.yml}
DC="docker compose $FILES"
WAIT_HEALTHY=${WAIT_HEALTHY:-120}      # сколько секунд ждать готовности новых реплик
GRACE=${STOP_GRACE:-45}                # grace-период остановки старой реплики

log() { printf '\033[1;34m[deploy]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[deploy]\033[0m %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null || die "docker не найден"

log "1/5 собираю образ"
$DC build api worker

mapfile -t OLD < <($DC ps -q api)
N=${#OLD[@]}
if [ "$N" -eq 0 ]; then
  log "api ещё не запущен — обычный старт"
  $DC up -d
  exit 0
fi
log "2/5 запущено реплик api: $N — поднимаю $N новых рядом"
$DC up -d --no-deps --no-recreate --scale api=$((N * 2)) api

log "3/5 жду готовности новых реплик (до ${WAIT_HEALTHY} с)"
deadline=$((SECONDS + WAIT_HEALTHY))
while :; do
  mapfile -t ALL < <($DC ps -q api)
  new=()
  for id in "${ALL[@]}"; do
    skip=0
    for o in "${OLD[@]}"; do [ "$id" = "$o" ] && skip=1; done
    [ $skip -eq 0 ] && new+=("$id")
  done
  ready=0
  for id in "${new[@]:-}"; do
    [ -n "$id" ] || continue
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$id" 2>/dev/null)" = "healthy" ] && ready=$((ready + 1))
  done
  [ "$ready" -ge "$N" ] && break
  if [ "$SECONDS" -ge "$deadline" ]; then
    log "новые реплики не стали healthy — откатываю (старые продолжают работать)"
    for id in "${new[@]:-}"; do [ -n "$id" ] && docker rm -f "$id" >/dev/null; done
    die "обновление отменено; смотрите: $DC logs --tail=100 api"
  fi
  sleep 3
done

log "4/5 останавливаю старые реплики по одной (grace ${GRACE} с)"
for id in "${OLD[@]}"; do
  docker stop -t "$GRACE" "$id" >/dev/null
  docker rm "$id" >/dev/null
  log "  заменена реплика ${id:0:12}"
  sleep 8   # Caddy обновляет список upstream раз в 5 секунд
done
$DC up -d --no-deps --scale api="$N" api >/dev/null

log "5/5 перезапускаю worker (последним)"
$DC up -d --no-deps worker
$DC up -d --no-deps caddy >/dev/null 2>&1 || true
log "готово"
$DC ps
