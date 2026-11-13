#!/usr/bin/env bash
# Холодный бэкап данных mnogobase в restic-репозиторий (шифрование, дедупликация, ротация).
# Простой: ~1–2 мин, пока api, worker и базы остановлены. Модели (GGUF) и кэши не копируются:
# они скачиваются заново.
#   APP_DIR=/srv/mnogobase/app deploy/backup.sh    (данные: $APP_DIR/data или MB_DATA)
# Репозиторий и пароль restic: /root/.restic-repo и /root/.restic-pass (или свои пути в env).
# MB_BACKUP_KEEP_STOPPED=1 — после удачного снимка не поднимать стек (это делает deploy/update.sh).
set -euo pipefail
app_dir="${APP_DIR:-/srv/mnogobase/app}"
cd "$app_dir"
data_dir="${MB_DATA:-$app_dir/data}"
export RESTIC_REPOSITORY_FILE="${RESTIC_REPOSITORY_FILE:-/root/.restic-repo}"
export RESTIC_PASSWORD_FILE="${RESTIC_PASSWORD_FILE:-/root/.restic-pass}"

running="$(docker compose ps --status running --services)"
restart() {
    local status=$?
    # MB_BACKUP_KEEP_STOPPED=1 (deploy/update.sh): после удачного снимка стек поднимает
    # вызывающий, уже с новым образом. После сбоя стек поднимается здесь в любом случае.
    if [ "$status" = 0 ] && [ "${MB_BACKUP_KEEP_STOPPED:-0}" = 1 ]; then return; fi
    # Поднимаем только то, что работало до бэкапа (в режиме Minecraft стек остановлен).
    # shellcheck disable=SC2086  # список сервисов разбивается на слова намеренно
    if [ -n "$running" ]; then docker compose up -d --no-recreate $running; fi
}
# trap ставится до stop: даже если скрипт упадёт посреди остановки или бэкапа, при выходе
# сервисы поднимутся снова, а не останутся лежать.
trap restart EXIT
# Базы останавливаются, чтобы копия их файлов была согласованной (холодный бэкап).
docker compose stop --timeout 120 api worker postgres qdrant neo4j

restic backup --tag mnogobase \
    "$data_dir/app" "$data_dir/postgres" "$data_dir/qdrant" "$data_dir/neo4j" \
    "$app_dir/.env" "$app_dir/config.yaml" \
    --exclude "$data_dir/app/.mnogobase/cache"
# Хранить 7 ежедневных, 4 еженедельных и 6 ежемесячных снимков; остальные удалить (--prune).
restic forget --tag mnogobase --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune
