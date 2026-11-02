"""Блокирующее ядро не занимает HTTP event loop и завершает запись при отмене."""

from __future__ import annotations

import asyncio
import threading

import pytest
from filelock import FileLock, Timeout

from mnogobase.config import Settings
from mnogobase.service.errors import ServiceError
from mnogobase.service.runtime import TeamService


@pytest.fixture
def service(tmp_path):
    service = TeamService(Settings(device="cpu", service={"root": tmp_path / "service"}))
    user = service.store.create_user("alice", "integration password")
    team = service.store.create_team("A")
    service.store.add_member(team["id"], user["id"], "admin")
    return service, service.store.context(user["id"], team["id"])


@pytest.mark.parametrize("operation", ["ask", "work_once"])
async def test_runtime_blocking_dependencies_leave_event_loop_responsive(
    service, monkeypatch, operation
):
    service, ctx = service
    entered, release = threading.Event(), threading.Event()
    worker_threads = []

    def blocking_dependency(*_):
        worker_threads.append(threading.get_ident())
        entered.set()
        if not release.wait(timeout=3):
            raise AssertionError("Bounded blocking dependency was never released")
        return False if operation == "ask" else []

    if operation == "ask":
        monkeypatch.setattr(service, "_has_sources", blocking_dependency)
        coroutine = service.ask(ctx, "Transformer")
    else:
        monkeypatch.setattr(service.store, "teams", blocking_dependency)
        coroutine = service.work_once()
    # Даже ошибочная реализация, блокирующая caller loop, завершится за ограниченное время.
    rescue = threading.Timer(0.7, release.set)
    rescue.daemon = True
    rescue.start()
    task = asyncio.create_task(coroutine)
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), timeout=3)
        await asyncio.sleep(0.02)
        assert not release.is_set(), "Synchronous core blocked the caller event loop"
        assert len(worker_threads) == 1
        assert worker_threads[0] != threading.get_ident()
    finally:
        release.set()
        rescue.cancel()
        result = await asyncio.wait_for(task, timeout=3)
    if operation == "work_once":
        assert result == 0
    else:
        assert result["sources"] == []


async def test_offload_core_runs_coroutines_in_distinct_private_loops_and_preserves_metadata():
    from mnogobase.service.async_ops import offload_core

    caller_loop = asyncio.get_running_loop()
    caller_thread = threading.get_ident()

    @offload_core
    async def operation(value: str, *, suffix: str = "!") -> tuple:
        """Operation metadata stays available to tools and introspection."""
        await asyncio.sleep(0)
        return value + suffix, asyncio.get_running_loop(), threading.get_ident()

    first = await operation("first", suffix="?")
    second = await operation("second")
    assert (first[0], second[0]) == ("first?", "second!")
    assert first[1] is not second[1]
    assert all(result[1] is not caller_loop and result[1].is_closed() for result in (first, second))
    assert all(result[2] != caller_thread for result in (first, second))
    assert operation.__name__ == "operation"
    assert operation.__doc__ == "Operation metadata stays available to tools and introspection."
    assert operation.__wrapped__.__name__ == "operation"


async def test_offload_core_propagates_service_errors():
    from mnogobase.service.async_ops import offload_core

    @offload_core
    async def denied():
        raise ServiceError(403, "Team access denied")

    with pytest.raises(ServiceError) as error:
        await denied()
    assert error.value.status == 403
    assert error.value.detail == "Team access denied"


async def test_cancelled_request_keeps_mutation_lock_until_worker_finishes(tmp_path):
    from mnogobase.service.async_ops import offload_core

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    lock_path = tmp_path / "corpus.lock"
    output = tmp_path / "mutation.txt"

    @offload_core
    async def mutate():
        try:
            with FileLock(lock_path, timeout=0):
                entered.set()
                if not release.wait(timeout=3):
                    raise AssertionError("Mutation was never released")
                await asyncio.sleep(0)
                output.write_text("complete", encoding="utf-8")
        finally:
            finished.set()

    rescue = threading.Timer(2, release.set)
    rescue.daemon = True
    rescue.start()
    task = asyncio.create_task(mutate())
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not finished.is_set()
        assert not output.exists()
        with pytest.raises(Timeout), FileLock(lock_path, timeout=0):
            pass
    finally:
        release.set()
        rescue.cancel()
        assert await asyncio.wait_for(asyncio.to_thread(finished.wait, 2), timeout=3)
        if not task.done():
            await asyncio.wait_for(task, timeout=3)
    with FileLock(lock_path, timeout=0):
        assert output.read_text(encoding="utf-8") == "complete"
