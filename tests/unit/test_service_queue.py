import sqlite3

import pytest

from mnogobase.background.queue import JobQueue


def test_typed_operations_survive_recovery_and_legacy_enqueues(tmp_path):
    path = tmp_path / "jobs.db"
    queue = JobQueue(path)
    ingest_id = queue.enqueue(tmp_path / "one.md", "content")
    operation_id = queue.enqueue(
        tmp_path / "two.md", "remove:two", kind="remove", payload_json='{"doc_id":"two"}'
    )
    assert queue.claim().id == ingest_id
    assert queue.claim().id == operation_id
    queue.close()
    queue = JobQueue(path)
    assert queue.recover() == 2
    assert queue.get(ingest_id).kind == "ingest"
    operation = queue.get(operation_id)
    assert operation.kind == "remove"
    assert operation.payload_json == '{"doc_id":"two"}'
    queue.close()


def test_concurrent_first_queue_open_migrates_once(tmp_path):
    import sqlite3
    import threading
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "jobs.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE jobs(id INTEGER PRIMARY KEY,path TEXT NOT NULL, fingerprint TEXT NOT NULL,"
            "status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,"
            "available_at REAL NOT NULL DEFAULT 0,error TEXT,progress TEXT NOT NULL DEFAULT '',"
            "UNIQUE(path,fingerprint))"
        )
    barrier = threading.Barrier(8)

    def enqueue(index):
        barrier.wait(timeout=3)
        queue = JobQueue(path)
        try:
            job_id = queue.enqueue(tmp_path / f"{index}.md", str(index))
            return queue.get(job_id).kind
        finally:
            queue.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(enqueue, range(8))) == ["ingest"] * 8


@pytest.mark.parametrize("retry", ["explicit", "all", "force"])
def test_failed_write_stays_dirty_when_attempts_are_reset(tmp_path, retry):
    queue = JobQueue(tmp_path / "jobs.db")
    source = tmp_path / "one.md"
    try:
        job_id = queue.enqueue(source, "content")
        assert not queue.has_unfinished_writes()
        queue.claim()
        queue.fail(job_id, "RuntimeError", max_attempts=1, delay=0)
        if retry == "force":
            queue.enqueue(source, "content", force=True)
        else:
            assert queue.retry(job_id if retry == "explicit" else None) == 1
        assert queue.get(job_id).attempts == 0
        assert queue.has_unfinished_writes()
    finally:
        queue.close()


def test_successful_current_remove_supersedes_failed_ingest_and_cleans_path(tmp_path):
    path = tmp_path / "jobs.db"
    queue = JobQueue(path)
    source = tmp_path / "one.md"
    try:
        ingest_id = queue.enqueue(source, "content")
        queue.claim()
        queue.fail(ingest_id, "RuntimeError", max_attempts=1, delay=0)
        remove_id = queue.enqueue(source, "remove:content", kind="remove")
        assert queue.claim().id == remove_id
        queue.finish(remove_id)
        assert queue.get(ingest_id).status == "superseded"
        assert queue.get(remove_id).status == "done"
        assert not queue.has_unfinished_writes()
    finally:
        queue.close()
    reopened = JobQueue(path)
    try:
        assert not reopened.has_unfinished_writes()
    finally:
        reopened.close()


def test_finishing_old_operation_keeps_newer_intent_dirty(tmp_path):
    queue = JobQueue(tmp_path / "jobs.db")
    source = tmp_path / "one.md"
    try:
        old_id = queue.enqueue(source, "old")
        old = queue.claim()
        current_id = queue.enqueue(source, "remove:old", kind="remove")
        assert not queue.is_current(old)
        assert queue.is_current(queue.get(current_id))
        queue.finish(old_id)
        assert queue.get(old_id).status == "done"
        assert queue.get(current_id).status == "pending"
        assert queue.has_unfinished_writes()
        assert queue.claim().id == current_id
        queue.finish(current_id)
        assert not queue.has_unfinished_writes()
    finally:
        queue.close()


@pytest.mark.parametrize("old_status", ["pending", "running", "failed"])
def test_current_finish_only_supersedes_unfinished_jobs_for_its_source(tmp_path, old_status):
    queue = JobQueue(tmp_path / "jobs.db")
    source = tmp_path / "one.md"
    try:
        old_id = queue.enqueue(source, "old")
        if old_status == "pending":
            queue.defer(old_id, 3600)
        else:
            queue.claim()
            if old_status == "failed":
                queue.fail(old_id, "RuntimeError", max_attempts=1, delay=0)
        other_id = queue.enqueue(tmp_path / "other.md", "other")
        assert queue.claim().id == other_id
        queue.fail(other_id, "RuntimeError", max_attempts=1, delay=0)
        current_id = queue.enqueue(source, "remove:old", kind="remove")
        assert queue.claim().id == current_id
        queue.finish(current_id)
        assert queue.get(old_id).status == "superseded"
        assert queue.get(other_id).status == "failed"
        assert queue.has_unfinished_writes()
        queue.retry(other_id)
        assert queue.claim().id == other_id
        queue.finish(other_id)
        assert not queue.has_unfinished_writes()
    finally:
        queue.close()


def test_crash_recover_and_retry_keep_dirty_marker_until_current_success(tmp_path):
    path = tmp_path / "jobs.db"
    queue = JobQueue(path)
    job_id = queue.enqueue(tmp_path / "one.md", "content")
    queue.claim()
    queue.close()
    queue = JobQueue(path)
    try:
        assert queue.recover() == 1
        assert queue.has_unfinished_writes()
        queue.claim()
        queue.fail(job_id, "RuntimeError", max_attempts=1, delay=0)
        queue.retry(job_id)
        assert queue.has_unfinished_writes()
        queue.claim()
        queue.finish(job_id)
        assert not queue.has_unfinished_writes()
    finally:
        queue.close()


def test_retry_restores_explicit_intent_and_all_retry_chooses_latest_failed(tmp_path):
    queue = JobQueue(tmp_path / "jobs.db")
    source = tmp_path / "one.md"
    try:
        first_id = queue.enqueue(source, "content")
        queue.claim()
        queue.fail(first_id, "RuntimeError", max_attempts=1, delay=0)
        second_id = queue.enqueue(source, "remove:content", kind="remove")
        queue.claim()
        queue.fail(second_id, "RuntimeError", max_attempts=1, delay=0)
        assert queue.retry(first_id) == 1
        assert queue.is_current(queue.get(first_id))
        assert not queue.is_current(queue.get(second_id))
        queue.claim()
        queue.fail(first_id, "RuntimeError", max_attempts=1, delay=0)
        assert queue.retry() == 2
        assert not queue.is_current(queue.get(first_id))
        assert queue.is_current(queue.get(second_id))
        assert queue.has_unfinished_writes()
    finally:
        queue.close()


def test_concurrent_existing_queue_migration_preserves_failed_write_state(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "jobs.db"
    queue = JobQueue(path)
    job_id = queue.enqueue(tmp_path / "one.md", "content")
    queue.claim()
    queue.fail(job_id, "RuntimeError", max_attempts=1, delay=0)
    queue.close()
    # An old queue has attempted jobs but no persistent write_state table yet.
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE IF EXISTS write_state")
    barrier = threading.Barrier(6)

    def open_retry(_):
        barrier.wait(timeout=3)
        connection = JobQueue(path)
        try:
            connection.retry(job_id)
            return connection.has_unfinished_writes()
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert list(pool.map(open_retry, range(6))) == [True] * 6
    queue = JobQueue(path)
    try:
        assert queue.get(job_id).attempts == 0
        assert queue.has_unfinished_writes()
        queue.claim()
        queue.finish(job_id)
        assert not queue.has_unfinished_writes()
    finally:
        queue.close()


def test_claim_rolls_back_running_status_if_marking_dirty_fails(tmp_path):
    queue = JobQueue(tmp_path / "jobs.db")
    try:
        job_id = queue.enqueue(tmp_path / "one.md", "content")
        queue._db.executescript("""
            CREATE TABLE IF NOT EXISTS write_state(path TEXT PRIMARY KEY);
            CREATE TRIGGER fail_dirty BEFORE INSERT ON write_state BEGIN
                SELECT RAISE(ABORT, 'dirty storage failed');
            END;
        """)
        with pytest.raises(sqlite3.IntegrityError, match="dirty storage failed"):
            queue.claim()
        assert queue.get(job_id).status == "pending"
        assert queue.get(job_id).attempts == 0
        assert not queue.has_unfinished_writes()
    finally:
        queue.close()


def test_finish_rolls_back_completion_if_clearing_dirty_fails(tmp_path):
    queue = JobQueue(tmp_path / "jobs.db")
    try:
        job_id = queue.enqueue(tmp_path / "one.md", "content")
        queue.claim()
        queue._db.executescript("""
            CREATE TABLE IF NOT EXISTS write_state(path TEXT PRIMARY KEY);
            INSERT OR IGNORE INTO write_state VALUES('test-existing-marker');
            CREATE TRIGGER fail_clean BEFORE DELETE ON write_state BEGIN
                SELECT RAISE(ABORT, 'dirty cleanup failed');
            END;
        """)
        with pytest.raises(sqlite3.IntegrityError, match="dirty cleanup failed"):
            queue.finish(job_id)
        assert queue.get(job_id).status == "running"
        assert queue.has_unfinished_writes()
    finally:
        queue.close()
