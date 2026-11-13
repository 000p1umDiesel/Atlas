"""Выполнение одной задачи очереди единственным писателем корпуса.

`process_one` вызывается в цикле из `mnogobase worker run` (`cli/jobs.py`), который держит
блокировку `worker` и передаёт сюда `handler` — функцию, выполняющую `ingest` или `remove`.
Каждая задача выполняется под advisory-блокировкой `ingest`, общей с CLI-писателями (`ingest`,
`remove`, `wiki build`, `reindex`, `reset`), поэтому корпус в каждый момент меняет кто-то один.

Исход задачи определяет тип ошибки:
- успех — `JobQueue.finish`;
- `TRANSIENT_ERRORS` (база, недоступные сервисы) — `release`: задача ждёт, попытка не
  засчитывается;
- превышение `timeout` — `fail` с ошибкой `JobTimeout`;
- любая другая ошибка — `fail` с именем типа исключения (повтор с удвоением задержки,
  потом `failed`).

Если процесс прерван посреди задачи (Ctrl+C, SIGTERM, крах), задача остаётся `running`;
её возвращает в очередь `JobQueue.recover` при следующем запуске worker'а.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from sqlalchemy.exc import DBAPIError

from mnogobase.core.database import DatabaseError, LockBusyError
from mnogobase.core.log import get_logger
from mnogobase.ops.graph_analysis import AnalysisError
from mnogobase.stores.registry import ingest_lock

from .queue import JobQueue


class ServicesUnavailable(RuntimeError):
    """Недоступна база или сервер моделей: задача ждёт, попытка не засчитывается."""


# Исключения, чей текст написан для пользователя и не содержит секретов: очередь хранит
# его целиком (иначе только имя типа), а задача сразу становится failed.
SAFE_MESSAGE_ERRORS: tuple[type[BaseException], ...] = (AnalysisError,)


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
    """Берёт и выполняет не больше одной готовой задачи.

    Аргументы: `handler(job)` — корутина, выполняющая задачу; `max_attempts` и `delay` —
    параметры повторов для `JobQueue.claim` / `fail` / `release`; `timeout` — предел времени
    на задачу в секундах (`None` — без предела); `on_transient(exc)` вызывается после
    инфраструктурного сбоя (worker по нему пересоздаёт `App`).

    Возвращает `True`, если задача была взята (с любым исходом), и `False`, если брать
    нечего или блокировку `ingest` держит другой писатель (CLI `ingest`, `reset` и т. п.) —
    тогда вызывающий просто ждёт и пробует снова. Ошибки обработчика не пробрасываются, а
    записываются в задачу; ошибки самой очереди (например, обрыв базы в `claim`)
    пробрасываются. `KeyboardInterrupt` и отмена тоже пробрасываются, оставляя задачу
    `running` до `recover`."""
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
        deadline = asyncio.timeout(timeout)  # None — без срока
        try:
            async with deadline:
                await handler(job)
        except TRANSIENT_ERRORS as exc:
            queue.release(job.id, delay)
            log.warning("job_deferred", job_id=job.id, error_type=type(exc).__name__)
            if on_transient is not None:
                on_transient(exc)
        except TimeoutError as exc:
            # JobTimeout — только истёкший `jobs.timeout_s`; TimeoutError из самого обработчика
            # (сокет, клиент модели) — обычная ошибка документа со своим типом
            error = "JobTimeout" if deadline.expired() else type(exc).__name__
            queue.fail(job.id, error, max_attempts=max_attempts, delay=delay)
            log.error("job_timeout" if deadline.expired() else "job_failed", job_id=job.id)
        except SAFE_MESSAGE_ERRORS as exc:
            # Текст написан для пользователя, а повтор даст тот же результат: задача сразу
            # failed, повторить её можно вручную (например, после установки GDS).
            queue.fail(job.id, f"{type(exc).__name__}: {exc}", max_attempts=1, delay=delay)
            log.warning("job_failed", job_id=job.id, error_type=type(exc).__name__)
        except Exception as exc:
            # Текст ошибки провайдера может содержать ключи и URL: сохраняем только тип.
            queue.fail(job.id, type(exc).__name__, max_attempts=max_attempts, delay=delay)
            log.warning("job_failed", job_id=job.id, error_type=type(exc).__name__)
        else:
            queue.finish(job.id)
        return True
    finally:
        lock.release()
