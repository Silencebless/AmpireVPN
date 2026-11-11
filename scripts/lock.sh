#!/usr/bin/env bash
# Генерирует requirements.lock — точные версии всех зависимостей с хэшами (воспроизводимые сборки).
# Запускать на машине с доступом в интернет; результат закоммитить. Dockerfile сам подхватит lock-файл.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m pip install --quiet pip-tools
python3 -m piptools compile --quiet --generate-hashes --resolver=backtracking \
  --output-file=requirements.lock requirements.in
echo "requirements.lock обновлён: $(grep -c '^[A-Za-z]' requirements.lock) пакетов"
