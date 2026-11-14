"""Heartbeat worker'а при долгой синхронной работе: поток задачи analyze и `HeartbeatTicker`."""

import asyncio
import gc
import threading
import time

import pytest
from structlog.testing import capture_logs

from mnogobase.cli.jobs import HeartbeatTicker, heartbeat_thread


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


class _Queue:
    """Очередь-заглушка для `HeartbeatTicker`: считает отметки, первые `failures` падают."""

    def __init__(self, log, failures=0):
        self.log, self.failures = log, failures

    def heartbeat(self):
        if self.failures:
            self.failures -= 1
            raise ConnectionError("db down")
        self.log.append("beat")

    def close(self):
        self.log.append("close")


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    return predicate()


def test_heartbeat_ticker_beats_only_while_busy():
    log, stop = [], threading.Event()
    ticker = HeartbeatTicker("target", stop, interval=0.01, open_queue=lambda t: _Queue(log))
    ticker.start()
    try:
        time.sleep(0.05)
        assert log == []  # вне задачи отмечает сам цикл worker'а
        with ticker.busy():
            assert _wait_for(lambda: log.count("beat") >= 3)
        time.sleep(0.03)  # отметка, начатая до выхода из блока, могла успеть
        quiet = len(log)
        time.sleep(0.05)
        assert len(log) == quiet
    finally:
        stop.set()
        ticker.join()
    assert log[-1] == "close"


def test_heartbeat_ticker_survives_database_errors_and_reopens():
    log, opened, stop = [], [], threading.Event()

    def open_queue(target):
        opened.append(target)
        return _Queue(log, failures=1 if len(opened) == 1 else 0)

    ticker = HeartbeatTicker("target", stop, interval=0.01, open_queue=open_queue)
    ticker.start()
    try:
        with capture_logs() as logs, ticker.busy():
            assert _wait_for(lambda: "beat" in log)
    finally:
        stop.set()
        ticker.join()
    assert len(opened) == 2  # после ошибки очередь закрыта и открыта заново
    assert log[0] == "close"
    assert {
        "event": "worker_heartbeat_failed",
        "error_type": "ConnectionError",
        "log_level": "warning",
    } in logs
