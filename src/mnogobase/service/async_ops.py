"""Асинхронная граница для блокирующего прикладного ядра."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any


def offload_core[**P, T](
    fn: Callable[P, Coroutine[Any, Any, T]],
) -> Callable[P, Coroutine[Any, Any, T]]:
    """Выполняет операцию в потоке с собственным циклом и временем жизни ресурсов.

    Отмена ожидания не прерывает запись: поток завершает операцию и освобождает
    её файловые блокировки. Между вызовами нет общих asyncio-примитивов.
    """

    @wraps(fn)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        return await asyncio.to_thread(lambda: asyncio.run(fn(*args, **kwargs)))

    return wrapped
