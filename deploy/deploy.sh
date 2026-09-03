#!/usr/bin/env bash
# Развёртывание на сервере: забрать из git, собрать, поднять, проверить.
#
# До этого код уезжал копированием по ssh — быстро, но неповторяемо: что
# именно крутится на сервере, приходилось выяснять сравнением файлов. Теперь
# сервер тянет из GitHub, и версия всегда равна коммиту.
#
#   bash deploy/deploy.sh            обычный деплой
#   bash deploy/deploy.sh --rollback откат на предыдущий коммит
set -euo pipefail

export DOCKER_HOST="${DOCKER_HOST:-unix:///home/igor/.docker/run/docker.sock}"
SRC="$HOME/workspace/dira-src"          # рабочая копия из git
RUN="$HOME/workspace/dira"              # что реально запущено: .env и data/
REPO="https://github.com/Igor-Lysk/dira-bot.git"

log() { echo "$(date '+%F %T') $*"; }

if [ ! -d "$SRC/.git" ]; then
    log "первый запуск: клонирую $REPO"
    git clone --depth 50 "$REPO" "$SRC"
fi

cd "$SRC"
PREVIOUS=$(git rev-parse --short HEAD)

if [ "${1:-}" = "--rollback" ]; then
    log "откат на предыдущий коммит"
    git reset --hard HEAD~1
else
    git fetch --quiet origin main
    git reset --hard --quiet origin/main
fi
CURRENT=$(git rev-parse --short HEAD)
log "версия: $PREVIOUS → $CURRENT ($(git log -1 --pretty=%s))"

# .env и база живут только в рабочей папке и в git не попадают
cp -r "$SRC"/{bot,core,collectors,db,extract,scripts,deploy,main.py,requirements.txt,Dockerfile,docker-compose.yml} "$RUN"/
cd "$RUN"

log "сборка и запуск"
docker compose up -d --build 2>&1 | tail -2

sleep 12
if ! docker ps --format '{{.Names}}' | grep -qx dira-bot; then
    log "КОНТЕЙНЕР НЕ ПОДНЯЛСЯ — откатываюсь"
    cd "$SRC" && git reset --hard --quiet "$PREVIOUS"
    cd "$RUN" && docker compose up -d --build 2>&1 | tail -2
    exit 1
fi

# Проверяем не «процесс жив», а что бот действительно доработал до готовности:
# контейнер может стоять и падать в цикле, и docker ps этого не покажет.
if docker logs --since 2m dira-bot 2>&1 | grep -q "планировщик запущен"; then
    log "готово, версия $CURRENT"
else
    log "внимание: в логах нет строки о запуске планировщика, проверь docker logs dira-bot"
fi
