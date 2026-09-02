#!/usr/bin/env bash
# Ночной бэкап базы с проверкой целостности.
#
# Копируется не файлом, а через sqlite3 backup API изнутри контейнера: при
# включённом WAL простой cp может застать базу в несогласованном состоянии.
# Каждая копия сразу проверяется integrity_check — бэкап, который не открылся,
# бесполезен, и узнать об этом лучше сейчас, а не в день, когда он понадобится.
set -euo pipefail

export DOCKER_HOST="${DOCKER_HOST:-unix:///home/igor/.docker/run/docker.sock}"
DIR="$HOME/workspace/dira"
BACKUPS="$DIR/data/backups"
KEEP_DAYS=14
STAMP=$(date +%Y-%m-%d)
TARGET="$BACKUPS/dira-$STAMP.db"
# Тот же файл, но как его видит контейнер: том смонтирован в /app/data.
# Внутри контейнера $HOME другой, поэтому путь передаём готовым.
TARGET_IN_CONTAINER="/app/data/backups/dira-$STAMP.db"

mkdir -p "$BACKUPS"

# -i обязателен: без него docker exec не пробрасывает stdin и python
# получает пустой скрипт, молча завершаясь с кодом 0.
docker exec -i dira-bot python - "$TARGET_IN_CONTAINER" <<'PY'
import sqlite3, sys
target = sys.argv[1]
src = sqlite3.connect("/app/data/dira.db")
dst = sqlite3.connect(target)
with dst:
    src.backup(dst)
dst.close(); src.close()

check = sqlite3.connect(target)
status = check.execute("PRAGMA integrity_check").fetchone()[0]
rows = check.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
check.close()
if status != "ok":
    raise SystemExit(f"бэкап повреждён: {status}")
print(f"бэкап в порядке: {rows} объявлений")
PY

gzip -f "$TARGET"
find "$BACKUPS" -name 'dira-*.db.gz' -mtime +$KEEP_DAYS -delete
echo "$(date '+%F %T') бэкап готов: $(ls -lh "$TARGET.gz" | awk '{print $5}'), всего копий: $(ls "$BACKUPS" | wc -l)"
