# PostgreSQL 15 + pgBackRest (непрерывный архив WAL, PITR) + redis-cli (метки свежести для мониторинга бота)
FROM postgres:15-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends pgbackrest redis-tools curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /var/spool/pgbackrest /var/log/pgbackrest /etc/pgbackrest /tmp/pgbackrest \
    && chown -R postgres:postgres /var/spool/pgbackrest /var/log/pgbackrest /etc/pgbackrest /tmp/pgbackrest
COPY deploy/backup/pgbackrest.conf /etc/pgbackrest/pgbackrest.conf
COPY deploy/backup/backup-loop.sh /usr/local/bin/backup-loop.sh
COPY deploy/backup/restore-test.sh /usr/local/bin/restore-test.sh
RUN chmod +x /usr/local/bin/backup-loop.sh /usr/local/bin/restore-test.sh
