from __future__ import annotations

import logging
import os
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
# structured tracebacks without frame locals: locals can hold API keys or document text
_TRACEBACKS = structlog.processors.ExceptionRenderer(
    structlog.tracebacks.ExceptionDictTransformer(show_locals=False)
)

# third-party loggers whose INFO lines bury the CLI progress display (warnings still pass)
_NOISY = ("httpx", "httpcore", "neo4j", "urllib3", "docling", "docling_core", "filelock")


class _WarningsOnly(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.WARNING


_WARNINGS_ONLY = _WarningsOnly()


class _CurrentStderr(logging.StreamHandler):
    """Writes to whatever `sys.stderr` is now: a Rich live display redirects it while it
    runs, so a warning is printed above the progress bars instead of through them."""

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stderr
        super().emit(record)


def _quiet_third_party() -> None:
    """Silence chatty INFO logs and model-loading progress bars of the parsing stack."""
    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)
    # RapidOCR (docling's OCR) logs INFO to its own handler and resets its level to INFO
    # when it is imported, so a filter is what keeps it quiet
    logging.getLogger("RapidOCR").addFilter(_WARNINGS_ONLY)
    # Hugging Face download bars and transformers' "Loading weights" bar
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    try:
        from transformers.utils import logging as hf_logging
    except ImportError:  # pragma: no cover - transformers is a dependency
        return
    hf_logging.disable_progress_bar()  # also disables huggingface_hub's bars


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
                _TRACEBACKS,
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
        console_handler.setLevel(logging.WARNING)  # Rich progress owns the terminal
        root.addHandler(console_handler)
    root.setLevel(level)
    _quiet_third_party()


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
