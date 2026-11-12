# Образ приложения mnogobase: API и worker (CPU). Модели — отдельные контейнеры llama.cpp.
FROM python:3.12-slim

# uv закреплённой версии: ставит зависимости строго по uv.lock.
COPY --from=ghcr.io/astral-sh/uv:0.11.15 /uv /usr/local/bin/uv

# opencv-python-headless (RapidOCR) обходится без libGL, но нужен glib.
# Приложение работает не от root, а от пользователя app с uid 10001: этот же uid
# deploy/prepare-data.sh назначает владельцем каталогов данных на Linux.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home app \
    && mkdir -p /app /data \
    && chown app:app /app /data

USER app
WORKDIR /app
# UV_LINK_MODE=copy — копировать пакеты из кэша uv (кэш смонтирован отдельно, жёсткие ссылки
# между ними не работают); UV_PYTHON_DOWNLOADS=never — использовать Python образа, не качать
# свой; PATH — команда mnogobase из .venv; PYTHONUNBUFFERED — логи сразу, без буфера;
# MNOGOBASE_LOG_FORMAT=json — логи в JSON для docker logs / journald; HF_HOME — кэш моделей
# Hugging Face (Docling) в смонтированном томе data/cache, чтобы не качать его заново.
ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    MNOGOBASE_LOG_FORMAT=json \
    HF_HOME=/home/app/.cache/huggingface

# Зависимости (~1,3 ГБ: torch, opencv, onnxruntime для Docling) — отдельным слоем, который
# зависит только от pyproject.toml и uv.lock: правка кода или README его не пересобирает.
# Кэш uv переживает пересборки, поэтому смена одной зависимости не качает всё заново.
# Байткод компилируется здесь один раз; второй sync ставит только сам mnogobase без него.
COPY --chown=app:app pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/home/app/.cache/uv,uid=10001,gid=10001 \
    uv sync --locked --no-install-project --compile-bytecode
COPY --chown=app:app README.md ./
COPY --chown=app:app src ./src
RUN --mount=type=cache,target=/home/app/.cache/uv,uid=10001,gid=10001 \
    uv sync --locked --no-compile-bytecode

# Относительные пути конфига (.mnogobase, wiki, documents, logs, runs) живут в томе /data;
# там же config.yaml, который compose монтирует только для чтения.
WORKDIR /data
# Команду задаёт compose.yaml (api serve или worker run); по умолчанию — только справка.
CMD ["mnogobase", "--help"]
