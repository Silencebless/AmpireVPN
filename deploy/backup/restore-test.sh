#!/usr/bin/env bash
# Тест восстановления: поднимаем копию из репозитория во ВРЕМЕННОМ каталоге, запускаем отдельный экземпляр
# PostgreSQL, проверяем, что данные читаются и свежие, и удаляем. Боевая база не затрагивается.
# Запуск вручную:  docker compose -f docker-compose.yml -f docker-compose.backup.yml exec pgbackrest restore-test.sh
set -uo pipefail

STANZA=${PGBACKREST_STANZA:-ampire}
DB_USER=${POSTGRES_USER:-ampire}
DB_NAME=${POSTGRES_DB:-ampire}
REDIS_HOST=${REDIS_HOST:-redis}
MAX_AGE_H=${RESTORE_MAX_DATA_AGE_H:-72}      # на сколько часов «позади» может быть восстановленная копия
DIR=$(mktemp -d /tmp/restore-test.XXXXXX)
PORT=5499
PGBIN=$(dirname "$(command -v pg_ctl || echo /usr/lib/postgresql/15/bin/pg_ctl)")

log() { printf '%s [restore-test] %s\n' "$(date -u +%FT%TZ)" "$*"; }
tg() {
  [ -n "${BOT_TOKEN:-}" ] || return 0
  for id in ${OWNER_IDS//,/ }; do
    curl -fsS -m 10 "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=$id" --data-urlencode "text=$1" >/dev/null 2>&1 || true
  done
}
cleanup() { "$PGBIN/pg_ctl" -D "$DIR/data" -m immediate stop >/dev/null 2>&1 || true; rm -rf "$DIR"; }
fail() { log "ОШИБКА: $1"; tg "🚨 AmpireVPN: ТЕСТ ВОССТАНОВЛЕНИЯ не прошёл — $1. Резервная копия может быть непригодна! См. docs/BACKUP.md"; cleanup; exit 1; }
trap cleanup EXIT

mkdir -p "$DIR/data" "$DIR/sock"
chmod 700 "$DIR/data"
log "восстанавливаю последнюю копию в $DIR/data"
pgbackrest --stanza="$STANZA" --pg1-path="$DIR/data" --type=immediate --target-action=promote restore \
  || fail "pgbackrest restore завершился ошибкой"

# отдельный экземпляр: другой порт, без сети, БЕЗ архивации (иначе он писал бы WAL в боевой репозиторий)
"$PGBIN/pg_ctl" -D "$DIR/data" -w -t 300 \
  -o "-p $PORT -c listen_addresses='' -c unix_socket_directories=$DIR/sock -c archive_mode=off -c archive_command=/bin/true" \
  start >/dev/null || fail "восстановленная база не запустилась"

Q() { psql -h "$DIR/sock" -p "$PORT" -U "$DB_USER" -d "$DB_NAME" -Atqc "$1"; }
users=$(Q "select count(*) from users") || fail "не читается таблица users"
pays=$(Q "select count(*) from payments") || fail "не читается таблица payments"
age_h=$(Q "select coalesce(extract(epoch from (now() - max(created_at)))/3600, 0)::int from users") || fail "не прочитать свежесть данных"
log "пользователей: $users, платежей: $pays, самая свежая регистрация: ${age_h} ч назад"
[ "${users:-0}" -gt 0 ] || fail "в восстановленной базе нет пользователей"
if [ "${age_h:-0}" -gt "$MAX_AGE_H" ]; then
  log "предупреждение: данные старше $MAX_AGE_H ч (возможно, в боте просто не было регистраций)"
fi

redis-cli -h "$REDIS_HOST" SET ampire:backup:restore_ok_ts "$(date +%s)" >/dev/null 2>&1 || log "метка в Redis не записана"
tg "✅ AmpireVPN: тест восстановления прошёл. Пользователей в копии: $users, платежей: $pays."
log "тест восстановления прошёл"
