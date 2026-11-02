"""Реальная PostgreSQL: контракты хранилищ, изоляция, конкуренция и миграция."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress

import pytest
from filelock import Timeout

from mnogobase.database import connect, database_lock, database_target

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
        assert a.replace_meta("signature", None, "one")
        assert not b.replace_meta("signature", None, "two")
        a.mark_dirty(["e1", "e2"])
        assert b.dirty() == ["e1", "e2"]
    finally:
        a.close()
        b.close()


def test_postgresql_service_sessions_and_private_chats(pg_settings):
    from mnogobase.service.errors import ServiceError
    from mnogobase.service.store import ServiceStore

    store = ServiceStore(database_target(pg_settings, "service"))
    alice, bob = [store.create_user(name, "password") for name in ("alice", "bob")]
    team = store.create_team("Team")
    for user in (alice, bob):
        store.add_member(team["id"], user["id"])
    a, b = [store.context(u["id"], team["id"]) for u in (alice, bob)]
    token = store.login("alice", "password")
    assert store.authenticate(token)["id"] == alice["id"]
    chat = store.create_conversation(a, "Приватный вопрос")
    message = store.append_message(a, chat["id"], "user", "? ' % : русский текст")
    assert store.messages(a, chat["id"]) == [message]
    assert store.conversations(b) == []
    with pytest.raises(ServiceError):
        store.messages(b, chat["id"])
    with pytest.raises(ServiceError):
        store.create_user("alice", "duplicate")
    assert store.create_user("carol", "password")
    store.logout(token)
    with pytest.raises(ServiceError):
        store.authenticate(token)


def test_postgresql_queue_concurrent_claims_are_unique(pg_settings, tmp_path):
    from mnogobase.background.queue import JobQueue

    target = database_target(pg_settings, "jobs")
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
    a = database_lock(pg_settings, "ingest", tmp_path / "a.lock")
    b = database_lock(pg_settings, "ingest", tmp_path / "b.lock")
    with a, pytest.raises(Timeout):
        b.acquire()
    with b:
        assert b.is_locked


def test_postgresql_corpus_snapshot_reads_registry(pg_settings):
    from mnogobase.processing import corpus_snapshot
    from mnogobase.registry import Registry

    registry = Registry(database_target(pg_settings))
    try:
        registry.upsert_file("/one.md", "one", 3, 1.0, "done")
        assert corpus_snapshot(pg_settings)["documents"] == 1
    finally:
        registry.close()


def test_sqlite_migration_preserves_all_stores_and_rejects_repeat(pg_settings, tmp_path):
    import hashlib

    from mnogobase.background.queue import JobQueue
    from mnogobase.database_migration import migrate_sqlite
    from mnogobase.registry import Registry
    from mnogobase.service.context import team_settings
    from mnogobase.service.runtime import TeamService
    from mnogobase.service.store import ServiceStore

    legacy = pg_settings.model_copy(deep=True)
    legacy.database.backend = "sqlite"
    service = TeamService(legacy)
    user = service.store.create_user("alice", "password")
    team = service.store.create_team("Team")
    service.store.add_member(team["id"], user["id"], "admin")
    ctx = service.store.context(user["id"], team["id"])
    token = service.store.login("alice", "password")
    chat = service.store.create_conversation(ctx)
    msg = service.store.append_message(ctx, chat["id"], "user", "Вопрос")
    service.upload(ctx, "paper.md", b"attention")
    scoped = team_settings(legacy, team["id"])
    registry = Registry(database_target(scoped))
    registry.upsert_file("/one.md", "one", 5, 1.123456789, "done")
    registry.put_extraction("c", "v1", "m", "old")
    registry.put_extraction("c", "v2", "m", "new")
    registry.put_extraction("c", "v1", "m", "latest")
    registry.set_meta("embedder", "unchanged")
    registry.close()
    files = list(tmp_path.rglob("*.db"))
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    report = migrate_sqlite(pg_settings)
    assert sum(report.values()) > 0
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files} == before
    store = ServiceStore(database_target(pg_settings, "service"))
    assert store.authenticate(token)["id"] == user["id"]
    assert store.messages(ctx, chat["id"]) == [msg]
    service = TeamService(pg_settings)
    assert service.documents(ctx)[0]["name"] == "paper.md"
    target = database_target(team_settings(pg_settings, team["id"]))
    registry = Registry(target)
    assert registry.get_latest_extraction("c") == "latest"
    assert registry.get_meta("embedder") == "unchanged"
    registry.put_extraction("c", "v2", "m", "after migration")
    assert registry.get_latest_extraction("c") == "after migration"
    registry.close()
    queue = JobQueue(target)
    new_id = queue.enqueue(tmp_path / "new.md", "new")
    assert new_id > 1
    queue.close()
    with pytest.raises(ValueError, match="empty|already|непуст"):
        migrate_sqlite(pg_settings)


def test_migration_rolls_back_all_imports_on_nonempty_destination(pg_settings):
    from mnogobase.database_migration import migrate_sqlite
    from mnogobase.registry import Registry
    from mnogobase.service.store import ServiceStore

    registry = Registry(pg_settings.data_dir / "state.db")
    registry.upsert_file("/one.md", "one", 1, 0, "done")
    registry.close()
    legacy_store = ServiceStore(pg_settings.service.root / "service.db")
    legacy_store.create_user("source", "password")
    destination = ServiceStore(database_target(pg_settings, "service"))
    destination.create_user("existing", "password")
    with pytest.raises(ValueError):
        migrate_sqlite(pg_settings)
    registry = Registry(database_target(pg_settings))
    assert registry.files() == []
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


def test_migration_late_constraint_failure_rolls_back_previous_tables(pg_settings):
    import sqlite3

    from sqlalchemy.exc import IntegrityError

    from mnogobase.database_migration import migrate_sqlite
    from mnogobase.service.store import ServiceStore

    source = ServiceStore(pg_settings.service.root / "service.db")
    source.create_user("alice", "password")
    with sqlite3.connect(source.path) as conn:
        conn.execute("INSERT INTO memberships VALUES('missing-team','missing-user','member')")
    with pytest.raises(IntegrityError):
        migrate_sqlite(pg_settings)
    target = connect(database_target(pg_settings, "service"))
    try:
        assert target.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    finally:
        target.close()


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

    target = database_target(pg_settings, "jobs")
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


def test_queue_open_cannot_restore_marker_from_finishing_job(pg_settings, tmp_path, monkeypatch):
    import threading
    from concurrent.futures import TimeoutError

    from mnogobase.background.queue import JobQueue
    from mnogobase.database import PgConnection

    target = database_target(pg_settings, "jobs")
    a = JobQueue(target)
    first = a.enqueue(tmp_path / "one.md", "A")
    a.claim()
    ready, proceed = threading.Event(), threading.Event()
    original = PgConnection.execute

    def interleave(db, sql, params=()):
        if "INSERT INTO write_state(path) SELECT DISTINCT" in sql:
            ready.set()
            assert proceed.wait(timeout=5)
        return original(db, sql, params)

    monkeypatch.setattr(PgConnection, "execute", interleave)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(JobQueue, target)
        ready.wait(timeout=0.5)
        a._db.execute("BEGIN")
        a._db.execute("UPDATE jobs SET status='done' WHERE id=?", (first,))
        a._db.execute("DELETE FROM write_state")
        proceed.set()
        try:
            future.result(timeout=0.5)
        except TimeoutError:
            pass
        finally:
            a._db.execute("COMMIT")
        b = future.result(timeout=5)
        try:
            assert b.get(first).status == "done"
            assert not b.has_unfinished_writes()
        finally:
            a.close()
            b.close()


def test_retry_does_not_reset_job_that_failed_after_selection(pg_settings, tmp_path, monkeypatch):
    from mnogobase.background.queue import JobQueue

    target = database_target(pg_settings, "jobs")
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
