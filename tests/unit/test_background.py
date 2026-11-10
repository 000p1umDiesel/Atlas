import asyncio
from pathlib import Path

import pytest

from mnogobase.config import Settings
from mnogobase.database import database_target
from mnogobase.registry import ingest_lock

pytestmark = pytest.mark.integration


def queue(path, clock=lambda: 100.0):
    from mnogobase.background.queue import JobQueue

    return JobQueue(database_target(Settings(), "jobs"), clock=clock)


def test_queue_persists_deduplicates_and_preserves_new_revision(tmp_path):
    db = tmp_path / "jobs.db"
    q = queue(db)
    first = q.enqueue(Path("a.pdf"), "v1")
    assert q.enqueue(Path("a.pdf"), "v1") == first
    running = q.claim()
    second = q.enqueue(Path("a.pdf"), "v2")
    q.finish(running.id)
    q.close()
    q = queue(db)
    assert q.claim().id == second
    assert q.jobs()[1].status == "done"
    q.close()


def test_claim_is_atomic_between_connections(tmp_path):
    a, b = queue(tmp_path / "q.db"), queue(tmp_path / "q.db")
    a.enqueue(Path("a.md"), "v1")
    assert a.claim() is not None
    assert b.claim() is None
    a.close()
    b.close()


def test_queue_recovers_crash_and_bounds_retries(tmp_path):
    now = [100.0]
    q = queue(tmp_path / "q.db", lambda: now[0])
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
    q = queue(tmp_path / "q.db")
    job_id = q.enqueue(Path("a.md"), "v1")
    q.finish(q.claim().id)
    q.enqueue(Path("a.md"), "v1")
    assert q.claim() is None
    q.enqueue(Path("a.md"), "v1", force=True)
    assert q.claim().id == job_id
    q.close()


def test_reverting_file_to_previous_content_is_a_new_request(tmp_path):
    q = queue(tmp_path / "q.db")
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
    q = queue(tmp_path / "q.db")
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
    q = queue(tmp_path / "q.db")
    watcher = Watcher(q, [tmp_path], ["md"], settle=0, excluded=[generated])
    watcher.scan()
    assert q.jobs() == []
    q.close()


async def test_worker_resumes_after_interruption_and_respects_writer_lock(tmp_path):
    from mnogobase.background.worker import process_one

    q = queue(tmp_path / "jobs.db")
    q.enqueue(Path("a.md"), "v1")

    async def interrupted(job):
        raise asyncio.CancelledError

    with ingest_lock(tmp_path, target=database_target(Settings())):
        assert not await process_one(q, interrupted, tmp_path)
    assert q.jobs()[0].attempts == 0
    with pytest.raises(asyncio.CancelledError):
        await process_one(q, interrupted, tmp_path)
    assert q.jobs()[0].status == "running"
    q.recover()
    completed = []

    async def handler(job):
        completed.append(job.id)

    assert await process_one(q, handler, tmp_path)
    assert q.jobs()[0].status == "done"
    assert len(completed) == 1
    q.close()


def test_recover_fails_jobs_that_keep_crashing_the_worker(tmp_path):
    q = queue(tmp_path / "q.db")
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
    q = queue(tmp_path / "q.db", lambda: now[0])
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
    q = queue(tmp_path / "jobs.db", lambda: now[0])
    q.enqueue(Path("a.md"), "v1")
    seen = []

    async def offline(job):
        raise ServicesUnavailable("models")

    assert await process_one(q, offline, tmp_path, delay=5, on_transient=seen.append)
    job = q.jobs()[0]
    assert (job.status, job.attempts) == ("pending", 0)
    assert isinstance(seen[0], ServicesUnavailable)

    async def hangs(job):
        await asyncio.sleep(10)

    now[0] += 10
    assert await process_one(q, hangs, tmp_path, max_attempts=1, timeout=0.05)
    job = q.jobs()[0]
    assert (job.status, job.error) == ("failed", "JobTimeout")
    q.close()


async def test_worker_records_failures_and_continues_other_files(tmp_path):
    from mnogobase.background.worker import process_one

    q = queue(tmp_path / "jobs.db")
    q.enqueue(Path("bad.md"), "v1")
    q.enqueue(Path("good.md"), "v1")

    async def handler(job):
        if job.path.endswith("bad.md"):
            raise ValueError("bad")

    await process_one(q, handler, tmp_path, max_attempts=1)
    await process_one(q, handler, tmp_path, max_attempts=1)
    assert {j.status for j in q.jobs()} == {"done", "failed"}
    assert any(j.error == "ValueError" for j in q.jobs())
    q.close()


async def test_watcher_can_enqueue_while_worker_holds_writer_lock(tmp_path):
    import threading

    from mnogobase.background.queue import JobQueue
    from mnogobase.background.watch import Watcher
    from mnogobase.background.worker import process_one
    from mnogobase.ids import file_doc_id

    source = tmp_path / "a.md"
    source.write_text("a")
    q = JobQueue(database_target(Settings(), "jobs"))
    q.enqueue(source, file_doc_id(source))

    async def handler(job):
        (tmp_path / "b.md").write_text("b")
        finished = threading.Event()

        def scan():
            other = JobQueue(database_target(Settings(), "jobs"))
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

    assert await process_one(q, handler, tmp_path)
    assert q.counts() == {"done": 1, "pending": 1}
    q.close()
