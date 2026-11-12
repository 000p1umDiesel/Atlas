#!/usr/bin/env bash
# Создаёт каталоги данных для compose.yaml и кладёт закреплённый плагин GDS.
#   deploy/prepare-data.sh            # ./data рядом с compose.yaml (локально и на сервере)
#   deploy/prepare-data.sh DIR        # другой каталог (MB_DATA, учебное восстановление)
set -euo pipefail
root="${1:-$(cd "$(dirname "$0")/.." && pwd)/data}"
gds_version=2.13.14
gds_sha256=24eef4e1ca6626770b1b0de465c9099db207b639f917146770fbed0e686661e9

# app — рабочий каталог api/worker (inbox — папка, за которой следит worker), cache — кэш
# моделей Docling, models — GGUF серверов llama.cpp, остальное — данные баз и плагины Neo4j.
mkdir -p "$root"/{app/inbox,cache,models,postgres,qdrant,neo4j,neo4j-plugins}
# Точка монтирования config.yaml внутри /data: без неё api и worker при первом старте
# создают её одновременно, и один из них падает («mountpoint … is outside of rootfs»).
touch "$root/app/config.yaml"
jar="$root/neo4j-plugins/neo4j-graph-data-science-$gds_version.jar"
# Плагин GDS скачивается один раз и проверяется по SHA-256, чтобы не запустить подменённый jar.
if [ ! -f "$jar" ]; then
    curl -fsSL -o "$jar.tmp" "https://graphdatascience.ninja/neo4j-graph-data-science-$gds_version.jar"
    actual=$( (command -v sha256sum >/dev/null && sha256sum "$jar.tmp" || shasum -a 256 "$jar.tmp") | cut -d" " -f1)
    if [ "$actual" != "$gds_sha256" ]; then
        rm -f "$jar.tmp"
        echo "Контрольная сумма jar GDS не совпадает: файл удалён, повторите загрузку" >&2
        exit 1
    fi
    mv "$jar.tmp" "$jar"
fi
# Приложение в контейнере работает от uid 10001 (Dockerfile). На Mac Docker Desktop сам
# сопоставляет владельцев, поэтому chown нужен только на Linux.
if [ "$(uname -s)" = Linux ]; then
    chown -R 10001:10001 "$root/app" "$root/cache"
fi
echo "Каталоги данных готовы: $root"
