"""Реальная PostgreSQL: контракты хранилищ, изоляция, конкуренция и миграция."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress

import pytest

from mnogobase.database import LockBusyError, database_lock, database_target

pytestmark = pytest.mark.integration


def test_postgresql_registry_preserves_cache_order_and_cas(pg_settings):
    from mnogobase.registry import Registry

    target = database_target(pg_settings)
    a, b = Registry(target), Registry(target)
    try:
        a.upsert_file("/doc.md", "doc", 2, 1.123456789, "done")
        assert b.get_file("/doc.md").mtime == 1.123456789
        for version, value in [("v1", "old"), ("v2", "new"), ("v1", "updated")]:
            a.put_extraction("c1", version, "model", value)
        assert b.get_latest_extraction("c1") == "updated"
        assert b.latest_extraction_versions() == {"v1"}
        assert a.set_meta_if_absent("signature", "one")
        assert not b.set_meta_if_absent("signature", "two")
        a.mark_dirty(["e1", "e2"])
        assert b.dirty() == ["e1", "e2"]
    finally:
        a.close()
        b.close()


def test_registry_migrates_file_size_to_bigint(pg_settings):
    from mnogobase.database import connect
    from mnogobase.registry import Registry

    target = database_target(pg_settings)
    Registry(target).close()
    db = connect(target)
    try:  # схема до миграции: версия 1, size INTEGER
        db.execute("ALTER TABLE files ALTER COLUMN size TYPE INTEGER")
        db.execute("UPDATE schema_migrations SET version=1 WHERE component='registry'")
    finally:
        db.close()
    registry = Registry(target)
    try:
        registry.upsert_file("/big.iso", "doc", 5 * 1024**3, 1.0, "done")
        assert registry.get_file("/big.iso").size == 5 * 1024**3
    finally:
        registry.close()
    db = connect(target)
    try:
        version = db.execute(
            "SELECT version FROM schema_migrations WHERE component='registry'"
        ).fetchone()[0]
        assert version == 2
    finally:
        db.close()


def test_lock_release_never_hides_the_original_error(pg_settings):
    lock = database_lock(pg_settings, "release-test")
    with pytest.raises(ValueError, match="original"), lock:
        lock.connection.raw.close()  # соединение оборвалось: unlock не выполнится
        raise ValueError("original")
    with database_lock(pg_settings, "release-test"):  # закрытое соединение отпустило lock
        pass


def test_postgresql_service_sessions_and_private_chats(pg_settings):
    from mnogobase.service.errors import ServiceError
    from mnogobase.service.store import ServiceStore

    store = ServiceStore(database_target(pg_settings))
    password = "long-enough-password"
    with pytest.raises(ServiceError, match="12"):
        store.create_user("short", "too-short")
    alice, bob = [store.create_user(name, password) for name in ("alice", "bob")]
    token = store.login("alice", password)
    me = store.authenticate(token)
    assert me == {"id": alice["id"], "username": "alice"}
    chat = store.create_conversation(alice["id"], "Приватный вопрос")
    message = store.append_message(alice["id"], chat["id"], "user", "? ' % : русский текст")
    assert store.messages(alice["id"], chat["id"]) == [message]
    assert store.conversations(bob["id"]) == []
    with pytest.raises(ServiceError):
        store.messages(bob["id"], chat["id"])
    with pytest.raises(ServiceError):
        store.create_user("alice", password)
    with pytest.raises(ServiceError):
        store.login("alice", "wrong-password-123")
    # новый пароль отзывает старые сессии
    store.set_password("alice", "another-long-password")
    with pytest.raises(ServiceError):
        store.authenticate(token)
    token = store.login("alice", "another-long-password")
    store.set_active("alice", False)
    with pytest.raises(ServiceError):
        store.authenticate(token)
    with pytest.raises(ServiceError):
        store.login("alice", "another-long-password")
    store.set_active("alice", True)
    token = store.login("alice", "another-long-password")
    store.logout(token)
    with pytest.raises(ServiceError):
        store.authenticate(token)
    assert [u["username"] for u in store.users()] == ["alice", "bob"]


def test_postgresql_queue_concurrent_claims_are_unique(pg_settings, tmp_path):
    from mnogobase.background.queue import JobQueue

    target = database_target(pg_settings)
    queue = JobQueue(target)
    try:
        for i in range(20):
            queue.enqueue(tmp_path / f"{i}.md", str(i))
    finally:
        queue.close()

    def claim(_):
        q = JobQueue(target)
        try:
            job = q.claim()
            return job.id if job else None
        finally:
            q.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(claim, range(20)))
    assert None not in ids
    assert len(set(ids)) == 20


def test_postgresql_locks_use_database_namespace_not_local_path(pg_settings, tmp_path):
    a = database_lock(pg_settings, "ingest")
    b = database_lock(pg_settings, "ingest")
    with a, pytest.raises(LockBusyError):
        b.acquire()
    with b:  # после освобождения lock берётся снова
        assert b.connection is not None


def test_postgresql_corpus_snapshot_reads_registry(pg_settings):
    from mnogobase.processing import corpus_snapshot
    from mnogobase.registry import Registry

    registry = Registry(database_target(pg_settings))
    try:
        registry.upsert_file("/one.md", "one", 3, 1.0, "done")
        assert corpus_snapshot(pg_settings)["documents"] == 1
    finally:
        registry.close()


def test_postgresql_project_marker_guards_wrong_cwd(pg_settings, tmp_path):
    from mnogobase.database import mark_project, project_exists
    from mnogobase.registry import Registry

    registry = Registry(database_target(pg_settings))
    registry.close()
    assert not project_exists(pg_settings)
    mark_project(pg_settings)
    assert project_exists(pg_settings)
    elsewhere = pg_settings.model_copy(update={"data_dir": tmp_path / "other"})
    assert not project_exists(elsewhere)


def test_postgresql_failed_transaction_can_be_reused(pg_settings):
    from sqlalchemy.exc import IntegrityError

    from mnogobase.registry import Registry

    registry = Registry(database_target(pg_settings))
    db = registry._db
    try:
        with pytest.raises(IntegrityError), db:
            db.execute("INSERT INTO meta VALUES('key','one')")
            db.execute("INSERT INTO meta VALUES('key','duplicate')")
        assert registry.get_meta("key") is None
        registry.set_meta("key", "after rollback")
        assert registry.get_meta("key") == "after rollback"
    finally:
        registry.close()


def test_enqueue_during_finish_remains_pending_and_dirty(pg_settings, tmp_path, monkeypatch):
    from concurrent.futures import TimeoutError

    from mnogobase.background.queue import JobQueue

    target = database_target(pg_settings)
    a, b = JobQueue(target), JobQueue(target)
    path = tmp_path / "one.md"
    first = a.enqueue(path, "A")
    a.claim()
    original = a._db.execute
    pending = []
    with ThreadPoolExecutor(max_workers=1) as pool:

        def interleave(sql, params=()):
            result = original(sql, params)
            if "SELECT 1 FROM desired" in sql:
                future = pool.submit(b.enqueue, path, "B")
                pending.append(future)
                # Correct path lock delays enqueue until finish commits.
                with suppress(TimeoutError):
                    future.result(timeout=0.5)
            return result

        monkeypatch.setattr(a._db, "execute", interleave)
        try:
            a.finish(first)
            second = pending[0].result(timeout=5)
            assert b.get(second).status == "pending"
            assert b.is_current(b.get(second))
        finally:
            a.close()
            b.close()


def test_retry_does_not_reset_job_that_failed_after_selection(pg_settings, tmp_path, monkeypatch):
    from mnogobase.background.queue import JobQueue

    target = database_target(pg_settings)
    a, b = JobQueue(target), JobQueue(target)
    path = tmp_path / "one.md"
    first = a.enqueue(path, "A")
    a.claim()
    a.enqueue(path, "B")
    original = a._db.execute
    triggered = False

    def interleave(sql, params=()):
        nonlocal triggered
        result = original(sql, params)
        if "FROM jobs WHERE status='failed'" in sql and not triggered:
            triggered = True
            b.fail(first, "error", max_attempts=1, delay=0)
        return result

    monkeypatch.setattr(a._db, "execute", interleave)
    try:
        assert a.retry(first) == 0
        assert a.get(first).status == "failed"
        assert a.retry(first) == 1
        assert a.is_current(a.get(first))
    finally:
        a.close()
        b.close()


def test_concurrent_project_marker_creation_is_atomic(pg_settings, monkeypatch):
    import threading
    from pathlib import Path

    from mnogobase.database import mark_project, project_exists
    from mnogobase.registry import Registry

    Registry(database_target(pg_settings)).close()
    barrier = threading.Barrier(2)
    original = Path.replace

    def interleave(path, target):
        if path.name.startswith("postgresql-project.") and path.suffix == ".tmp":
            barrier.wait(timeout=3)
        return original(path, target)

    monkeypatch.setattr(Path, "replace", interleave)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: mark_project(pg_settings), range(2)))
    assert project_exists(pg_settings)
