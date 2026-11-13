"""Долгая синхронная работа worker'а в потоке с heartbeat (задача analyze)."""

import asyncio
import gc
import threading

import pytest
from structlog.testing import capture_logs

from mnogobase.cli.jobs import heartbeat_thread


async def test_heartbeat_thread_returns_the_result_and_ticks():
    ticks = []
    assert await heartbeat_thread(lambda: 42, lambda: ticks.append(1), interval=0.01) == 42
    assert ticks


async def test_cancelled_thread_is_logged_once_it_finishes():
    """После таймаута задачи поток не прервать: его исход пишется событием лога, а не
    сообщением asyncio «exception was never retrieved» с трейсбеком."""
    release = threading.Event()
    unhandled = []
    asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: unhandled.append(ctx))

    def slow():
        release.wait(5)
        raise RuntimeError("bolt://user:secret@host")

    with capture_logs() as logs:
        task = asyncio.create_task(heartbeat_thread(slow, lambda: None, interval=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        for _ in range(100):
            if logs:
                break
            await asyncio.sleep(0.01)
    gc.collect()
    await asyncio.sleep(0)
    assert logs == [
        {
            "event": "orphan_thread_finished",
            "error_type": "RuntimeError",
            "log_level": "warning",
        }
    ]
    assert unhandled == []
