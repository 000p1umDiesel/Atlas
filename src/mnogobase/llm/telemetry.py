"""Учёт отдельных попыток вызова провайдера без текста промптов.

`OpenAICompatLLM` на каждую попытку запроса создаёт `CallEvent` (`new_call`) и отправляет
его получателю (`record_call`): сначала со статусом `started`, затем тот же `id` со статусом
`response` (с токенами) или `error`. В `build_app` получатель — `UsageStore.record`, таблица
`llm_usage` в PostgreSQL. Ни текст сообщений, ни ответ модели сюда не попадают — только
модель, задача, время, токены и статус.

`job_scope(job_id)` помечает вызовы внутри блока номером задачи очереди: worker оборачивает
им обработку задачи, и расход можно отнести к конкретному ingest.
"""

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
    """Одна попытка вызова LLM.

    `id` — общий для событий `started` и итогового одной попытки (получатель обновляет по
    нему строку); `operation` — задача (`extract`, `answer`, …); `created_at` — Unix-время
    начала попытки; `status` — `started` / `response` / `error`; токены известны только для
    `response` (и то, если сервер их сообщил); `job_id` — задача очереди или `None`;
    `external` — модель за внешним API (`serve: api`), для неё считается стоимость.
    """

    id: str
    model: str
    operation: str
    created_at: float
    status: str = "started"
    tokens_in: int | None = None
    tokens_out: int | None = None
    job_id: int | None = None
    external: bool = True


# номер задачи очереди, к которой относятся вызовы LLM текущей asyncio-задачи / потока
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
    """Новое событие `started` со свежим `id`, текущим временем и `job_id` из `job_scope`."""
    return CallEvent(
        uuid4().hex, model, operation, time.time(), job_id=_JOB.get(), external=external
    )


async def record_call(
    event: CallEvent | None, recorder: Callable[[CallEvent], None] | None = None
) -> None:
    """Передаёт `event` синхронному `recorder` в отдельном потоке, чтобы запись в БД не
    блокировала event loop. Без получателя или события ничего не делает; исключения
    получателя пробрасываются — их глотает вызывающий (`OpenAICompatLLM._record`)."""
    if recorder is not None and event is not None:
        await asyncio.to_thread(recorder, event)
