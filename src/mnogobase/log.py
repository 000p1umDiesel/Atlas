from __future__ import annotations

import logging
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog

_SHARED = [
    structlog.contextvars.merge_contextvars,
    structlog.processors.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", key="ts"),
]


def configure_logging(logs_dir: Path, level: str = "INFO", console: bool = True) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    structlog.configure(
        processors=[*_SHARED, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    file_handler = RotatingFileHandler(
        logs_dir / "mnogobase.jsonl", maxBytes=10_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.dict_tracebacks,
                structlog.processors.JSONRenderer(ensure_ascii=False),
            ],
            foreign_pre_chain=_SHARED,
        )
    )
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.addHandler(file_handler)
    if console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.dev.ConsoleRenderer(),
                ],
                foreign_pre_chain=_SHARED,
            )
        )
        console_handler.setLevel(logging.WARNING)  # Rich progress owns the terminal
        root.addHandler(console_handler)
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "neo4j", "urllib3", "docling", "filelock"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None):
    return structlog.get_logger(name)


def new_run_id() -> str:
    run_id = uuid.uuid4().hex[:12]
    structlog.contextvars.bind_contextvars(run_id=run_id)
    return run_id


def bind_context(**kv) -> None:
    structlog.contextvars.bind_contextvars(**kv)


def unbind_context(*keys: str) -> None:
    structlog.contextvars.unbind_contextvars(*keys)


@contextmanager
def log_stage(logger, stage: str, **fields) -> Iterator[None]:
    start = time.perf_counter()
    try:
        yield
    except Exception as exc:
        logger.error(
            "stage_failed",
            stage=stage,
            duration_ms=int((time.perf_counter() - start) * 1000),
            error_type=type(exc).__name__,
            error=str(exc),
            exc_info=True,
            **fields,
        )
        raise
    logger.info(
        "stage_done", stage=stage, duration_ms=int((time.perf_counter() - start) * 1000), **fields
    )
