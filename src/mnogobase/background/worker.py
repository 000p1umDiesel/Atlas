"""Single-writer dispatch; interrupted jobs remain running for startup recovery."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from sqlalchemy.exc import DBAPIError

from mnogobase.core.database import DatabaseError, LockBusyError
from mnogobase.core.log import get_logger
from mnogobase.stores.registry import ingest_lock

from .queue import JobQueue


class ServicesUnavailable(RuntimeError):
    """Недоступна база или сервер моделей: задача ждёт, попытка не засчитывается."""


# Сбои инфраструктуры, а не документа: задача возвращается в очередь без расхода попытки.
TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    ServicesUnavailable,
    DatabaseError,
    DBAPIError,
)


async def process_one(
    queue: JobQueue,
    handler,
    *,
    max_attempts: int = 3,
    delay: float = 10,
    timeout: float | None = None,
    on_transient: Callable[[BaseException], None] | None = None,
) -> bool:
    log = get_logger(__name__)
    lock = ingest_lock(queue.target)
    try:
        lock.acquire()
    except LockBusyError:
        return False
    try:
        job = queue.claim(max_attempts)
        if job is None:
            return False
        try:
            if timeout is None:
                await handler(job)
            else:
                await asyncio.wait_for(handler(job), timeout)
        except TRANSIENT_ERRORS as exc:
            queue.release(job.id, delay)
            log.warning("job_deferred", job_id=job.id, error_type=type(exc).__name__)
            if on_transient is not None:
                on_transient(exc)
        except TimeoutError:
            queue.fail(job.id, "JobTimeout", max_attempts=max_attempts, delay=delay)
            log.error("job_timeout", job_id=job.id, timeout_s=timeout)
        except Exception as exc:
            # Raw provider errors can contain credentials; persist only the type.
            queue.fail(job.id, type(exc).__name__, max_attempts=max_attempts, delay=delay)
            log.warning("job_failed", job_id=job.id, error_type=type(exc).__name__)
        else:
            queue.finish(job.id)
        return True
    finally:
        lock.release()
