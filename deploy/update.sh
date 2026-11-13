#!/usr/bin/env bash
# Обновление mnogobase на сервере одной командой: снимок restic → git pull → пересборка образа →
# перезапуск api и worker.
#   sudo deploy/update.sh                  # ветка main
#   sudo deploy/update.sh feature/x        # другая ветка с origin
#   sudo deploy/update.sh --no-backup      # без снимка (правки без изменений схемы и данных)
# Перед снимком проверяется, что ветка есть на origin, обновляется перемоткой (fast-forward)
# и что в рабочей копии нет локальных правок: так скрипт не останавливает стек ради
# обновления, которое всё равно не получится.
# git работает от имени владельца каталога (admin), а не root: иначе новые файлы стали бы
# принадлежать root и следующий `git pull` споткнулся бы о права.
# Образ собирается как mnogobase:latest — его поднимает mnogobase.service после перезагрузки.
# Прежний образ сохраняется под тегом mnogobase:<прежний коммит> для быстрого отката.
# Если стек остановлен (режим Minecraft в host-mode), образ только собирается, а запустится
# при следующем `host-mode mnogobase`.
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

    # 1. Проверки до остановки чего-либо.
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

    # 2. Снимок текущего состояния (код и данные до обновления).
    if [ "$backup" = 1 ]; then
        echo "==> Снимок restic (api, worker и базы остановятся на несколько минут)"
        APP_DIR="$app_dir" "$app_dir/deploy/backup.sh"
    fi

    # 3. Код: переключиться на ветку и перемотать её до origin.
    echo "==> git: $old → $branch ($new)"
    as_owner git switch --quiet "$branch"
    as_owner git merge --ff-only --quiet "origin/$branch"
    as_owner git log --oneline --no-decorate "$old..HEAD" | head -30

    # 4. Образ: прежний сохраняется для отката, новый собирается как latest.
    if docker image inspect mnogobase:latest >/dev/null 2>&1; then
        docker tag mnogobase:latest "mnogobase:$old"
    fi
    echo "==> Сборка образа"
    docker compose build api

    # 5. Перезапуск — только если стек работал (в режиме Minecraft он остановлен).
    if grep -qx api <<<"$running"; then
        echo "==> Перезапуск api и worker"
        docker compose up -d --wait
        docker compose ps --format 'table {{.Service}}\t{{.Status}}'
    else
        echo "Стек mnogobase не запущен (режим Minecraft?): образ собран, запустится при"
        echo "следующем \`host-mode mnogobase\`"
    fi
    echo "==> Готово: $branch на $(as_owner git rev-parse --short HEAD)."
    echo "Откат кода и образа: sudo -u $owner git checkout $old &&"
    echo "  sudo docker tag mnogobase:$old mnogobase:latest && sudo docker compose up -d --wait"
}

main "$@"
exit $?
