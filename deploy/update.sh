#!/usr/bin/env bash
# Обновление mnogobase на сервере одной командой: git pull → сборка образа → снимок restic →
# перезапуск api и worker с новым образом.
#   sudo deploy/update.sh                  # ветка main
#   sudo deploy/update.sh feature/x        # другая ветка с origin
#   sudo deploy/update.sh --no-backup      # без снимка (правки без изменений схемы и данных)
# Пока идут проверки, pull и сборка, сайт работает: если ветки нет, она не перематывается
# (fast-forward), в рабочей копии есть локальные правки или сборка упала, стек не трогается.
# Если образ не изменился, снимок и перезапуск не нужны. Иначе стек останавливается один раз:
# снимок restic (до миграций — они идут при старте новых контейнеров) и сразу запуск с новым
# образом.
# git работает от имени владельца каталога (admin), а не root: иначе новые файлы стали бы
# принадлежать root и следующий `git pull` споткнулся бы о права.
# Образ собирается как mnogobase:latest — его поднимает mnogobase.service после перезагрузки.
# Прежний образ сохраняется под тегом mnogobase:<прежний коммит> для быстрого отката.
# Если стек остановлен (режим Minecraft в host-mode), образ собирается и снимок делается, а
# запустится стек при следующем `host-mode mnogobase`.
set -euo pipefail

# Весь код — в функции: `git pull` переписывает и этот файл, а bash читает скрипт по ходу
# выполнения. Функция разбирается целиком до запуска, поэтому новая версия файла на неё
# не влияет.
main() {
    local backup=1 branch=main
    while [ $# -gt 0 ]; do
        case "$1" in
            --no-backup) backup=0 ;;
            -h | --help)
                # справка — первый блок комментариев файла
                awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
                return 0
                ;;
            -*)
                echo "Неизвестный параметр: $1" >&2
                return 2
                ;;
            *) branch="$1" ;;
        esac
        shift
    done

    if [ "$(id -u)" != 0 ]; then
        echo "Запустите через sudo: docker и бэкап требуют прав root" >&2
        return 2
    fi
    local app_dir owner
    app_dir="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
    cd "$app_dir"
    owner="$(ls -ld "$app_dir" | awk '{print $3}')"
    as_owner() { sudo -u "$owner" -- "$@"; }

    git check-ref-format --branch "$branch" >/dev/null || {
        echo "Некорректное имя ветки: $branch" >&2
        return 2
    }

    # 1. Проверки.
    if [ -n "$(as_owner git status --porcelain --untracked-files=no)" ]; then
        echo "В $app_dir есть локальные правки отслеживаемых файлов (git status):" >&2
        echo "закоммитьте или отмените их, иначе git pull их перезапишет или упадёт" >&2
        return 1
    fi
    as_owner git fetch --prune origin
    if ! as_owner git rev-parse --verify --quiet "refs/remotes/origin/$branch" >/dev/null; then
        echo "Ветки $branch нет на origin" >&2
        return 1
    fi
    local old new
    old="$(as_owner git rev-parse --short HEAD)"
    new="$(as_owner git rev-parse --short "origin/$branch")"
    if as_owner git show-ref --verify --quiet "refs/heads/$branch" \
        && ! as_owner git merge-base --is-ancestor "$branch" "origin/$branch"; then
        echo "Локальная ветка $branch разошлась с origin/$branch (force-push?): разберитесь вручную" >&2
        return 1
    fi
    local running
    running="$(docker compose ps --status running --services)"

    # 2. Код: переключиться на ветку и перемотать её до origin.
    echo "==> git: $old → $branch ($new)"
    as_owner git switch --quiet "$branch"
    as_owner git merge --ff-only --quiet "origin/$branch"
    if [ "$old" = "$new" ]; then
        echo "Новых коммитов нет"
    else
        as_owner git log --oneline --no-decorate "$old..HEAD" | head -30
    fi

    # 3. Образ — пока стек работает. Метка MB_REVISION — коммит, из которого он собран.
    local was_running=0 current="" after prev_rev
    if grep -qx api <<<"$running"; then
        was_running=1
        # образ, на котором api работает сейчас (может отличаться от latest после сбоя)
        current="$(docker inspect --format '{{.Image}}' "$(docker compose ps -q api)")"
    else
        current="$(docker image inspect --format '{{.Id}}' mnogobase:latest 2>/dev/null || true)"
    fi
    echo "==> Сборка образа (сайт пока работает)"
    if ! MB_REVISION="$(as_owner git rev-parse --short HEAD)" docker compose build api; then
        echo "Сборка не удалась, стек не тронут. Код уже на $new: исправьте и запустите снова" >&2
        echo "или верните код: sudo -u $owner git checkout $old" >&2
        return 1
    fi
    after="$(docker image inspect --format '{{.Id}}' mnogobase:latest)"
    if [ "$current" = "$after" ]; then
        echo "==> Образ не изменился: снимок и перезапуск не нужны. $branch на $new."
        return 0
    fi
    # Заменяемый образ остаётся под тегом своего коммита (у образов без метки — прежний HEAD).
    prev_rev="$old"
    if [ -n "$current" ]; then
        local label
        label="$(docker image inspect \
            --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$current")"
        case "$label" in "" | unknown | "<no value>") ;; *) prev_rev="$label" ;; esac
        docker tag "$current" "mnogobase:$prev_rev"
    fi

    # 4. Снимок и перезапуск: стек останавливается один раз.
    if [ "$backup" = 1 ]; then
        echo "==> Снимок restic$([ "$was_running" = 1 ] && echo ' (сайт недоступен до конца перезапуска)')"
        # После удачного снимка backup.sh не поднимает стек сам: это сразу делает шаг ниже,
        # уже с новым образом. Если снимок упадёт, backup.sh поднимет стек как был.
        if ! MB_BACKUP_KEEP_STOPPED=1 APP_DIR="$app_dir" "$app_dir/deploy/backup.sh"; then
            echo "Снимок не удался: стек поднят на прежнем образе, новый уже собран." >&2
            echo "Разберитесь с restic и запустите снова (или с --no-backup)." >&2
            return 1
        fi
    fi
    if [ "$was_running" = 1 ]; then
        echo "==> Запуск с новым образом"
        if ! docker compose up -d --wait; then
            echo "Стек не поднялся: sudo docker compose ps; sudo docker compose logs --tail 50 api worker" >&2
            return 1
        fi
        docker compose ps --format 'table {{.Service}}\t{{.Status}}'
    else
        echo "Стек mnogobase не был запущен (режим Minecraft?): образ собран, запустится при"
        echo "следующем \`host-mode mnogobase\`"
    fi
    echo "==> Готово: $branch на $(as_owner git rev-parse --short HEAD)."
    echo "Откат кода и образа: sudo -u $owner git checkout $prev_rev &&"
    echo "  sudo docker tag mnogobase:$prev_rev mnogobase:latest && sudo docker compose up -d --wait"
    echo "Миграции базы назад не откатываются: если они были, восстановите снимок restic."
}

main "$@"
exit $?
