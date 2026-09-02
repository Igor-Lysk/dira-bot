#!/usr/bin/env bash
# Сторож контейнера.
#
# `restart: unless-stopped` поднимает контейнер после падения, но не после того,
# как rootless-демон Docker сам не стартовал — а именно это в v1 оставило бота
# молчать пять дней. Поэтому проверяем оба уровня: жив ли демон и жив ли
# контейнер, и поднимаем то, чего не хватает.
set -uo pipefail

export DOCKER_HOST="${DOCKER_HOST:-unix:///home/igor/.docker/run/docker.sock}"
DIR="$HOME/workspace/dira"
LOG="$DIR/data/watchdog.log"
say() { echo "$(date '+%F %T') $*" >> "$LOG"; }

if ! docker info >/dev/null 2>&1; then
    say "docker не отвечает — пробую поднять пользовательский демон"
    systemctl --user start docker >/dev/null 2>&1 || say "systemctl --user start docker не сработал"
    sleep 10
fi

if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx dira-bot; then
    say "контейнер не запущен — поднимаю"
    (cd "$DIR" && docker compose up -d >>"$LOG" 2>&1) && say "поднят" || say "поднять не удалось"
fi

# Ротация собственного лога, чтобы он не рос бесконечно
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 2000 ]; then
    tail -500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
