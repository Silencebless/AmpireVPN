#!/usr/bin/env bash
# Планировщик резервных копий (контейнер pgbackrest):
#   • раз в сутки в BACKUP_HOUR (UTC-часы контейнера): воскресенье — полная копия, остальные дни — разностная;
#   • непрерывный архив WAL ведёт сам PostgreSQL (archive_command) — RPO измеряется секундами, а не сутками;
#   • после каждой успешной копии пишет метку свежести в Redis (бот алертит, если метки нет дольше BACKUP_MAX_AGE_H);
#   • раз в месяц делает ТЕСТ ВОССТАНОВЛЕНИЯ (restore-test.sh): копия, которую не проверяли, копией не считается.
set -uo pipefail

STANZA=${PGBACKREST_STANZA:-ampire}
HOUR=${BACKUP_HOUR_UTC:-1}
REDIS_HOST=${REDIS_HOST:-redis}
KEY_OK=ampire:backup:pitr_ok_ts
STATE=/tmp/pgbackrest/last_backup_day

log() { printf '%s [backup] %s\n' "$(date -u +%FT%TZ)" "$*"; }
mark() { redis-cli -h "$REDIS_HOST" SET "$1" "$(date +%s)" >/dev/null 2>&1 || log "не удалось записать метку $1 в Redis"; }
tg() {   # уведомление владельцам напрямую через Telegram API (бот может быть недоступен)
  [ -n "${BOT_TOKEN:-}" ] || return 0
  for id in ${OWNER_IDS//,/ }; do
    curl -fsS -m 10 "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=$id" --data-urlencode "text=$1" >/dev/null 2>&1 || true
  done
}

until pg_isready -h /var/run/postgresql -q; do log "жду PostgreSQL…"; sleep 5; done

log "инициализация репозитория (stanza=$STANZA)"
pgbackrest --stanza="$STANZA" stanza-create || log "stanza-create: возможно, уже создан"
if ! pgbackrest --stanza="$STANZA" check; then
  tg "🚨 AmpireVPN: pgBackRest check не прошёл — архив WAL/бэкапы НЕ работают. См. docs/BACKUP.md"
  log "check не прошёл (проверьте S3-ключи, endpoint, пароль шифрования, archive_command)"
fi

while true; do
  today=$(date -u +%F)
  hour=$(date -u +%-H)
  if [ "$hour" -ge "$HOUR" ] && [ "$(cat "$STATE" 2>/dev/null || true)" != "$today" ]; then
    if [ "$(date -u +%u)" = "7" ]; then type=full; else type=diff; fi
    log "резервная копия: $type"
    if pgbackrest --stanza="$STANZA" --type="$type" backup; then
      echo "$today" > "$STATE"
      mark "$KEY_OK"
      log "копия $type готова"
    else
      log "копия $type НЕ удалась — повторю через 10 минут"
      tg "⚠️ AmpireVPN: резервная копия ($type) не удалась. Повторяю. См. docs/BACKUP.md"
    fi
  fi
  # тест восстановления: 2-го числа каждого месяца после 04:00 UTC (раз в месяц)
  if [ "$(date -u +%-d)" = "2" ] && [ "$(date -u +%-H)" -ge 4 ] && [ "$(cat /tmp/pgbackrest/last_restore_test 2>/dev/null || true)" != "$(date -u +%Y-%m)" ]; then
    if /usr/local/bin/restore-test.sh; then
      date -u +%Y-%m > /tmp/pgbackrest/last_restore_test
    fi
  fi
  sleep 600
done
