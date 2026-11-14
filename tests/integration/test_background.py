import asyncio
from pathlib import Path

import pytest

from mnogobase.core.config import Settings
from mnogobase.core.database import database_target
from mnogobase.stores.registry import ingest_lock

pytestmark = pytest.mark.integration


def queue(clock=lambda: 100.0):
    from mnogobase.background.queue import JobQueue

    return JobQueue(database_target(Settings()), clock=clock)


def test_queue_persists_deduplicates_and_preserves_new_revision(tmp_path):
    q = queue()
    first = q.enqueue(Path("a.pdf"), "v1")
    assert q.enqueue(Path("a.pdf"), "v1") == first
    running = q.claim()
    second = q.enqueue(Path("a.pdf"), "v2")
    q.finish(running.id)
    q.close()
    q = queue()
    assert q.claim().id == second
    assert q.jobs()[1].status == "done"
    q.close()


def test_claim_is_atomic_between_connections(tmp_path):
    a, b = queue(), queue()
    a.enqueue(Path("a.md"), "v1")
    assert a.claim() is not None
    assert b.claim() is None
    a.close()
    b.close()


def test_queue_recovers_crash_and_bounds_retries(tmp_path):
    now = [100.0]
    q = queue(lambda: now[0])
    job_id = q.enqueue(Path("a.md"), "v1")
    assert q.claim().attempts == 1
    assert q.recover() == 1
    job = q.claim()
    q.fail(job.id, "offline", max_attempts=3, delay=10)
    assert q.claim() is None
    now[0] += 20
    job = q.claim()
    assert job.attempts == 3
    q.fail(job.id, "offline", max_attempts=3, delay=10)
    assert q.jobs()[0].status == "failed"
    assert q.retry(job_id) == 1
    assert q.claim().attempts == 1
    q.close()


def test_force_requeues_completed_content(tmp_path):
    q = queue()
    job_id = q.enqueue(Path("a.md"), "v1")
    q.finish(q.claim().id)
    q.enqueue(Path("a.md"), "v1")
    assert q.claim() is None
    q.enqueue(Path("a.md"), "v1", force=True)
    assert q.claim().id == job_id
    q.close()


def test_removed_source_is_not_requeued_without_force(tmp_path):
    """Файл из inbox, забытый через API, не возвращается после рестарта worker'а
    (watcher снова ставит его в очередь без force); явный force по-прежнему работает."""
    q = queue()
    ingest = q.enqueue(Path("a.md"), "v1")
    q.finish(q.claim().id)
    q.enqueue(Path("a.md"), "remove:v1", force=True, kind="remove")
    q.finish(q.claim().id)
    q.enqueue(Path("a.md"), "v1")  # watcher после рестарта
    assert q.claim() is None
    q.enqueue(Path("a.md"), "v1", force=True)
    assert q.claim().id == ingest
    q.close()


def test_clear_forgets_queue_history(tmp_path):
    q = queue()
    q.enqueue(Path("a.md"), "v1")
    q.finish(q.claim().id)
    q.clear()
    assert q.jobs() == []
    q.enqueue(Path("a.md"), "v1")  # после reset тот же файл снова загружается
    assert q.claim() is not None
    q.close()


def test_reverting_file_to_previous_content_is_a_new_request(tmp_path):
    q = queue()
    first = q.enqueue(Path("a.md"), "v1")
    q.finish(q.claim().id)
    q.enqueue(Path("a.md"), "v2")
    q.finish(q.claim().id)
    q.enqueue(Path("a.md"), "v1")
    job = q.claim()
    assert job is not None and job.id == first
    q.finish(job.id)
    q.enqueue(Path("a.md"), "v1")
    assert q.claim() is None
    q.close()


def test_watcher_waits_for_stability_deduplicates_and_recovers_restart(tmp_path):
    from mnogobase.background.watch import Watcher

    now = [0.0]
    root = tmp_path / "docs"
    root.mkdir()
    source = root / "a.md"
    source.write_text("first")
    (root / ".hidden.md").write_text("hidden")
    (root / "ignore.exe").write_text("unsupported")
    q = queue()
    watcher = Watcher(q, [root], ["md"], settle=2, clock=lambda: now[0])
    assert watcher.scan() == 0
    now[0] = 1
    source.write_text("second version")
    assert watcher.scan() == 0
    now[0] = 3
    assert watcher.scan() == 1
    assert watcher.scan() == 0
    restarted = Watcher(q, [root], ["md"], settle=2, clock=lambda: now[0])
    restarted.scan()
    now[0] = 5
    restarted.scan()
    assert len(q.jobs()) == 1
    source.write_text("new content during processing")
    q.claim()
    watcher.scan()
    now[0] = 7
    watcher.scan()
    assert len(q.jobs()) == 2
    source.unlink()
    assert watcher.scan() == 0
    q.close()


def test_watcher_excludes_generated_directories_and_symlinks(tmp_path):
    from mnogobase.background.watch import Watcher

    generated = tmp_path / "wiki"
    generated.mkdir()
    (generated / "a.md").write_text("generated")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text("external")
    (tmp_path / "link.md").symlink_to(outside)
    q = queue()
    watcher = Watcher(q, [tmp_path], ["md"], settle=0, excluded=[generated])
    watcher.scan()
    assert q.jobs() == []
    q.close()


async def test_worker_resumes_after_interruption_and_respects_writer_lock(tmp_path):
    from mnogobase.background.worker import process_one

    q = queue()
    q.enqueue(Path("a.md"), "v1")

    async def interrupted(job):
        raise asyncio.CancelledError

    with ingest_lock(database_target(Settings())):
        assert not await process_one(q, interrupted)
    assert q.jobs()[0].attempts == 0
    with pytest.raises(asyncio.CancelledError):
        await process_one(q, interrupted)
    assert q.jobs()[0].status == "running"
    q.recover()
    completed = []

    async def handler(job):
        completed.append(job.id)

    assert await process_one(q, handler)
    assert q.jobs()[0].status == "done"
    assert len(completed) == 1
    q.close()


def test_recover_fails_jobs_that_keep_crashing_the_worker(tmp_path):
    q = queue()
    q.enqueue(Path("crash.pdf"), "v1")
    q.enqueue(Path("fine.pdf"), "v1")
    for _ in range(2):
        assert q.claim(2).path.endswith("crash.pdf")
        q.recover(2)  # процесс упал посреди задачи
    jobs = {Path(j.path).name: j for j in q.jobs()}
    assert jobs["crash.pdf"].status == "failed"
    assert jobs["crash.pdf"].error == "InterruptedTooOften"
    assert q.claim(2).path.endswith("fine.pdf")
    q.close()


def test_release_does_not_spend_an_attempt(tmp_path):
    now = [100.0]
    q = queue(lambda: now[0])
    q.enqueue(Path("a.md"), "v1")
    job = q.claim()
    q.release(job.id, 30)
    assert q.claim() is None
    now[0] += 31
    assert q.claim().attempts == 1
    q.close()


async def test_worker_defers_on_unavailable_services_and_times_out(tmp_path):
    from mnogobase.background.worker import ServicesUnavailable, process_one

    now = [100.0]
    q = queue(lambda: now[0])
    q.enqueue(Path("a.md"), "v1")
    seen = []

    async def offline(job):
        raise ServicesUnavailable("models")

    assert await process_one(q, offline, delay=5, on_transient=seen.append)
    job = q.jobs()[0]
    assert (job.status, job.attempts) == ("pending", 0)
    assert isinstance(seen[0], ServicesUnavailable)

    async def hangs(job):
        await asyncio.sleep(10)

    now[0] += 10
    assert await process_one(q, hangs, max_attempts=1, timeout=0.05)
    job = q.jobs()[0]
    assert (job.status, job.error) == ("failed", "JobTimeout")
    q.close()


async def test_timeout_inside_the_handler_is_not_reported_as_job_timeout(tmp_path):
    """Таймаут сокета внутри обработчика — обычная ошибка документа, а не истёкший срок задачи."""
    from mnogobase.background.worker import process_one

    q = queue()
    q.enqueue(Path("a.md"), "v1")

    async def socket_timeout(job):
        raise TimeoutError("read timed out")

    assert await process_one(q, socket_timeout, max_attempts=1, timeout=60)
    assert q.jobs()[0].error == "TimeoutError"
    q.close()


async def test_worker_records_failures_and_continues_other_files(tmp_path):
    from mnogobase.background.worker import process_one

    q = queue()
    q.enqueue(Path("bad.md"), "v1")
    q.enqueue(Path("good.md"), "v1")

    async def handler(job):
        if job.path.endswith("bad.md"):
            raise ValueError("bad")

    await process_one(q, handler, max_attempts=1)
    await process_one(q, handler, max_attempts=1)
    assert {j.status for j in q.jobs()} == {"done", "failed"}
    assert any(j.error == "ValueError" for j in q.jobs())
    q.close()


async def test_watcher_can_enqueue_while_worker_holds_writer_lock(tmp_path):
    import threading

    from mnogobase.background.queue import JobQueue
    from mnogobase.background.watch import Watcher
    from mnogobase.background.worker import process_one
    from mnogobase.core.ids import file_doc_id

    source = tmp_path / "a.md"
    source.write_text("a")
    q = JobQueue(database_target(Settings()))
    q.enqueue(source, file_doc_id(source))

    async def handler(job):
        (tmp_path / "b.md").write_text("b")
        finished = threading.Event()

        def scan():
            other = JobQueue(database_target(Settings()))
            try:
                Watcher(other, [tmp_path], ["md"], settle=0).scan()
            finally:
                other.close()
                finished.set()

        thread = threading.Thread(target=scan)
        thread.start()
        assert await asyncio.to_thread(finished.wait, 2)
        thread.join()
        assert len(q.jobs()) == 2

    assert await process_one(q, handler)
    assert q.counts() == {"done": 1, "pending": 1}
    q.close()


def test_cancel_pending_remove_restores_desired(tmp_path):
    q = queue()
    q.enqueue(Path("a.md"), "d1")
    q.finish(q.claim().id)
    removal = q.enqueue(Path("a.md"), "remove:d1", force=True, kind="remove")
    assert q.cancel(removal).status == "cancelled"
    assert q.claim() is None
    assert q.is_current(q.find(Path("a.md"), "d1"))
    q.close()


def test_cancel_loses_to_a_claimed_job(tmp_path):
    a, b = queue(), queue()
    job_id = a.enqueue(Path("a.md"), "d1")
    assert b.claim().id == job_id
    assert a.cancel(job_id) is None
    assert a.get(job_id).status == "running"
    a.close()
    b.close()


def test_cancelled_ingest_is_not_requeued_by_the_watcher(tmp_path):
    q = queue()
    job_id = q.enqueue(Path("a.md"), "d1")
    q.cancel(job_id)
    assert q.enqueue(Path("a.md"), "d1") == job_id  # watcher, без force
    assert q.claim() is None
    changed = q.enqueue(Path("a.md"), "d2")  # содержимое изменилось
    assert q.claim().id == changed
    q.close()


def test_cancelled_ingest_returns_with_force(tmp_path):
    q = queue()
    job_id = q.enqueue(Path("a.md"), "d1")
    q.cancel(job_id)
    assert q.enqueue(Path("a.md"), "d1", force=True) == job_id  # повторная загрузка через API
    assert q.claim().id == job_id
    q.close()


def test_cancel_runs_on_locked_and_rolls_back_on_error(tmp_path):
    q = queue()
    job_id = q.enqueue(Path("a.md"), "d1")

    def broken(job):
        raise OSError("disk")

    with pytest.raises(OSError):
        q.cancel(job_id, on_locked=broken)
    assert q.get(job_id).status == "pending"
    seen = []
    q.cancel(job_id, on_locked=seen.append)
    assert [j.id for j in seen] == [job_id]
    q.close()


def test_hide_finished_keeps_failed_and_unhides_requeued(tmp_path):
    q = queue()
    done = q.enqueue(Path("a.md"), "d1")
    q.finish(q.claim().id)
    failed = q.enqueue(Path("b.md"), "d2")
    q.fail(q.claim().id, "Boom", max_attempts=1, delay=0)
    cancelled = q.enqueue(Path("c.md"), "d3")
    q.cancel(cancelled)
    assert q.hide_finished() == 2
    assert [j.id for j in q.jobs()] == [failed]
    assert len(q.jobs(include_hidden=True)) == 3
    q.enqueue(Path("a.md"), "d1", force=True)
    assert {j.id for j in q.jobs()} == {done, failed}
    q.close()


def test_retry_unhides_nothing_it_did_not_hide(tmp_path):
    """`retry` возвращает `failed`-задачу, а скрытые соседние задачи остаются скрытыми."""
    q = queue()
    q.enqueue(Path("a.md"), "d1")
    q.finish(q.claim().id)
    failed = q.enqueue(Path("b.md"), "d2")
    q.fail(q.claim().id, "Boom", max_attempts=1, delay=0)
    q.hide_finished()
    assert q.retry(failed) == 1
    assert [j.id for j in q.jobs()] == [failed]
    q.close()


def test_pending_ids_lists_copies_of_a_removal(tmp_path):
    q = queue()
    a = q.enqueue(Path("a.md"), "remove:d1", force=True, kind="remove")
    b = q.enqueue(Path("b.md"), "remove:d1", force=True, kind="remove")
    assert q.pending_ids("remove:d1") == [a, b]
    q.close()
