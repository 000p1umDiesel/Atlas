"""Учёт отдельных попыток вызова провайдера без текста промптов."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True)
class CallEvent:
    id: str
    model: str
    operation: str
    created_at: float
    status: str = "started"
    tokens_in: int | None = None
    tokens_out: int | None = None
    job_id: int | None = None
    external: bool = True


_JOB: ContextVar[int | None] = ContextVar("llm_usage_job", default=None)


@contextmanager
def job_scope(job_id: int | None) -> Iterator[None]:
    """Привязывает вызовы LLM внутри блока к задаче очереди."""
    token = _JOB.set(job_id)
    try:
        yield
    finally:
        _JOB.reset(token)


def new_call(model: str, operation: str, *, external: bool) -> CallEvent:
    return CallEvent(
        uuid4().hex, model, operation, time.time(), job_id=_JOB.get(), external=external
    )


async def record_call(
    event: CallEvent | None, recorder: Callable[[CallEvent], None] | None = None
) -> None:
    if recorder is not None and event is not None:
        await asyncio.to_thread(recorder, event)
