"""Структурированные логи (structlog поверх stdlib logging) и маскировка секретов.

`configure_logging` вызывается один раз при старте CLI, API или worker'а: все записи —
свои и сторонних библиотек — уходят JSON-строками в `logs/mnogobase.jsonl` (локально) или
в stdout (контейнеры, `MNOGOBASE_LOG_FORMAT=json`); в терминал локально идут только
warning и выше, чтобы не мешать Rich progress. Модули получают логгер через `get_logger`
и пишут события коротким snake_case-именем с полями (`stage_done`, `lock_release_failed`).

Контекст запуска (`run_id`, документ, стадия) привязывается через contextvars
(`new_run_id`, `bind_context`) и автоматически попадает во все записи. Тексты ошибок
перед записью в БД и логи проходят через `safe_error`, который вырезает ключи и пароли;
traceback пишутся без локальных переменных по той же причине.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog

# процессоры для всех записей, включая записи сторонних библиотек через stdlib logging
_SHARED = [
    structlog.contextvars.merge_contextvars,
    structlog.processors.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", key="ts"),
]
# структурированные traceback без локальных переменных фреймов: в них могут быть
# API-ключи или текст документов
_TRACEBACKS = structlog.processors.ExceptionRenderer(
    structlog.tracebacks.ExceptionDictTransformer(show_locals=False)
)

# сторонние логгеры, чьи INFO-строки забивают прогресс в CLI (warning по-прежнему проходят)
_NOISY = ("httpx", "httpcore", "neo4j", "urllib3", "docling", "docling_core", "filelock")


class _WarningsOnly(logging.Filter):
    """Пропускает только записи уровня WARNING и выше."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.WARNING


_WARNINGS_ONLY = _WarningsOnly()


class _CurrentStderr(logging.StreamHandler):
    """Пишет в тот `sys.stderr`, который актуален сейчас: Rich live display подменяет его,
    пока работает, поэтому warning печатается над прогресс-барами, а не поверх них."""

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stderr
        super().emit(record)


def _quiet_third_party() -> None:
    """Глушит болтливые INFO-логи и прогресс-бары загрузки моделей в стеке парсинга."""
    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)
    # RapidOCR (OCR в docling) пишет INFO в свой handler и при импорте сбрасывает уровень
    # на INFO, поэтому заглушить его можно только фильтром
    logging.getLogger("RapidOCR").addFilter(_WARNINGS_ONLY)
    # прогресс-бары загрузки Hugging Face и бар "Loading weights" из transformers
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    try:
        from transformers.utils import logging as hf_logging
    except ImportError:  # pragma: no cover - transformers есть в зависимостях
        return
    hf_logging.disable_progress_bar()  # заодно отключает бары huggingface_hub


def _json_formatter() -> logging.Formatter:
    """Форматтер одной JSON-строки на запись: и для structlog, и для чужих stdlib-логгеров."""
    return structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            _TRACEBACKS,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        foreign_pre_chain=_SHARED,
    )


def configure_logging(logs_dir: Path, level: str = "INFO", console: bool = True) -> None:
    """JSON-журнал в `logs_dir/mnogobase.jsonl` с ротацией (локальный CLI).

    С `MNOGOBASE_LOG_FORMAT=json` (контейнеры) журнал идёт в stdout: его собирает и
    ротирует `docker logs`, а несколько процессов не делят один файл.

    Локально файл ротируется по 10 МБ, хранится 5 старых копий. `console=True` добавляет
    вывод warning и выше в stderr в читаемом виде. Прежние обработчики корневого логгера
    снимаются, так что повторный вызов (тесты, вложенные команды) не дублирует записи.
    Заодно глушит шумные сторонние логгеры (`_quiet_third_party`)."""
    structlog.configure(
        processors=[*_SHARED, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    to_stdout = os.environ.get("MNOGOBASE_LOG_FORMAT") == "json"
    if to_stdout:
        main_handler: logging.Handler = logging.StreamHandler(sys.stdout)
    else:
        logs_dir.mkdir(parents=True, exist_ok=True)
        main_handler = RotatingFileHandler(
            logs_dir / "mnogobase.jsonl", maxBytes=10_000_000, backupCount=5, encoding="utf-8"
        )
    main_handler.setFormatter(_json_formatter())
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.addHandler(main_handler)
    if console and not to_stdout:
        console_handler = _CurrentStderr(sys.stderr)
        console_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.dev.ConsoleRenderer(),
                ],
                foreign_pre_chain=_SHARED,
            )
        )
        console_handler.setLevel(logging.WARNING)  # терминалом владеет Rich progress
        root.addHandler(console_handler)
    root.setLevel(level)
    _quiet_third_party()


def get_logger(name: str | None = None):
    """structlog-логгер модуля; обычно `_log = get_logger(__name__)` на уровне модуля.

    Конфигурация применяется при первом использовании, поэтому вызывать можно до
    `configure_logging`."""
    return structlog.get_logger(name)


def new_run_id() -> str:
    """Создаёт id запуска (12 hex-символов) и привязывает его к контексту логов: все
    последующие записи этого потока / задачи asyncio несут поле `run_id`."""
    run_id = uuid.uuid4().hex[:12]
    structlog.contextvars.bind_contextvars(run_id=run_id)
    return run_id


# (шаблон, замена) для `safe_error`: Bearer-токены, ключи вида sk-..., пароль в URL
# (`://user:pass@` -> `://***@`) и пары `key=value` / `"key": "value"` с секретными именами
_SECRETS = (
    (re.compile(r"(?i)\bbearer\s+[^\s\"',;]+"), "Bearer ***"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), "sk-***"),
    (re.compile(r"(://)[^/\s:@]+:[^/\s@]+@"), r"\1***@"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|token|password|passwd|secret|key)"
            r"(\"?\s*[:=]\s*\"?)[^\s\"'&,;}]+"
        ),
        r"\1\2***",
    ),
)


def safe_error(exc: BaseException, limit: int = 500) -> str:
    """`Тип: сообщение` для БД и логов: ошибки провайдера и HTTP-клиентов могут содержать
    ключи (Bearer, sk-..., пароль в URL, api_key=...); они заменяются на ***, а длина
    ограничена."""
    text = f"{type(exc).__name__}: {exc}"
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def bind_context(**kv) -> None:
    """Добавляет поля во все следующие записи текущего контекста (поток / задача asyncio)."""
    structlog.contextvars.bind_contextvars(**kv)


def unbind_context(*keys: str) -> None:
    """Убирает поля `keys` из контекста логов."""
    structlog.contextvars.unbind_contextvars(*keys)


@contextmanager
def log_stage(logger, stage: str, **fields) -> Iterator[None]:
    """Контекстный менеджер стадии: пишет `stage_done` с длительностью в мс при успехе или
    `stage_failed` с типом, безопасным текстом ошибки и traceback при исключении.

    Исключение пробрасывается дальше без изменений; `fields` добавляются в обе записи.
    `BaseException` вне `Exception` (отмена, Ctrl+C) не логируется."""
    start = time.perf_counter()
    try:
        yield
    except Exception as exc:
        logger.error(
            "stage_failed",
            stage=stage,
            duration_ms=int((time.perf_counter() - start) * 1000),
            error_type=type(exc).__name__,
            error=safe_error(exc),
            exc_info=True,
            **fields,
        )
        raise
    logger.info(
        "stage_done", stage=stage, duration_ms=int((time.perf_counter() - start) * 1000), **fields
    )
