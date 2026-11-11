FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/mpl

# postgresql-client нужен для pg_dump (зашифрованный дамп из админки); версия 15 совпадает с postgres:15 в compose
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Если рядом лежит requirements.lock (pip-compile --generate-hashes, см. scripts/lock.sh), ставим СТРОГО по нему:
# одинаковые версии и проверка хэшей на каждой сборке. Иначе — по границам из requirements.txt.
COPY requirements.txt requirements.lock* ./
RUN if [ -f requirements.lock ]; then pip install --require-hashes -r requirements.lock; else pip install -r requirements.txt; fi

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

RUN useradd -m -u 10001 bot && chown -R bot /app
USER bot

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.getenv('WEB_PORT','8080'), timeout=3)" || exit 1

STOPSIGNAL SIGTERM
CMD ["python", "-m", "app"]
