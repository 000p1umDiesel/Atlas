import asyncio
import signal
import threading
from types import SimpleNamespace

import pytest
from rich.progress import Progress
from sqlalchemy.exc import ResourceClosedError
from typer.testing import CliRunner

from mnogobase import cli
from mnogobase.background.queue import JobQueue
from mnogobase.cli import jobs as background_cli
from mnogobase.core.config import Settings
from mnogobase.core.database import DatabaseTarget, database_lock, database_target
from mnogobase.core.ids import file_doc_id

pytestmark = pytest.mark.integration

runner = CliRunner()


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.common, "configure_logging", lambda *a: None)
    source = tmp_path / "a.md"
    source.write_text("document")
    return source


@pytest.fixture
def tracked_queues(monkeypatch):
    queues = []

    class TrackedQueue(JobQueue):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.owner = threading.current_thread().name
            self.before_close = None
            self.closed = False
            queues.append(self)

        def close(self):
            if self.before_close is not None:
                self.before_close()
            super().close()
            with pytest.raises(ResourceClosedError):
                self.counts()
            self.closed = True

    monkeypatch.setattr(background_cli, "JobQueue", TrackedQueue)
    return queues


def test_enqueue_list_and_retry_without_models(project, monkeypatch):
    monkeypatch.setattr(cli.common, "build_app", lambda *a: pytest.fail("models constructed"))
    result = runner.invoke(cli.app, ["jobs", "enqueue", str(project)])
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli.app, ["jobs", "list"])
    assert result.exit_code == 0, result.output
    assert "pending" in result.output and "a.md" in result.output
    result = runner.invoke(cli.app, ["jobs", "retry"])
    assert result.exit_code == 0, result.output


def test_worker_once_processes_queue_and_closes_app(project, monkeypatch):
    calls, closed, snapshots = [], [], []

    async def ingest(paths, **options):
        calls.extend(paths)
        observer = JobQueue(database_target(Settings()))
        try:
            progress = options["progress"]
            progress.files_found(1)
            progress.file_started(str(paths[0]))
            progress.step("parse", 1)
            snapshots.append(observer.jobs()[0].progress)
            progress.advance()
            snapshots.append(observer.jobs()[0].progress)
            progress.file_done("processed")
        finally:
            observer.close()
        return SimpleNamespace(failed={}, wiki=None, processed=paths, skipped=[])

    app = SimpleNamespace(pipeline=SimpleNamespace(ingest=ingest), close=lambda: closed.append(1))
    monkeypatch.setattr(cli.common, "run_checks", lambda *a, **k: [])
    monkeypatch.setattr(cli.common, "build_app", lambda *a: app)
    result = runner.invoke(cli.app, ["jobs", "enqueue", str(project)])
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli.app, ["worker", "run", "--once"])
    assert result.exit_code == 0, result.output
    assert calls == [project] and closed == [1]
    assert snapshots == ["parse 0/1; ошибок 0", "parse 1/1; ошибок 0"]
    result = runner.invoke(cli.app, ["jobs", "list"])
    assert "done" in result.output


def test_second_worker_and_busy_writer_leave_job_unclaimed(project):
    runner.invoke(cli.app, ["jobs", "enqueue", str(project)])
    with database_lock(Settings(), "worker"):
        result = runner.invoke(cli.app, ["worker", "run", "--once"])
    assert result.exit_code == 2
    with database_lock(Settings(), "ingest"):
        result = runner.invoke(cli.app, ["worker", "run", "--once"])
    assert result.exit_code == 1, result.output
    from mnogobase.background.queue import JobQueue

    q = JobQueue(database_target(Settings()))
    assert q.jobs()[0].attempts == 0
    q.close()


def test_worker_wiki_failure_remains_retryable(project, monkeypatch):
    async def ingest(paths, **options):
        return SimpleNamespace(failed={}, wiki=SimpleNamespace(failed=["page"]))

    monkeypatch.setattr(cli.common, "run_checks", lambda *a, **k: [])
    monkeypatch.setattr(
        cli.common,
        "build_app",
        lambda *a: SimpleNamespace(pipeline=SimpleNamespace(ingest=ingest), close=lambda: None),
    )
    runner.invoke(cli.app, ["jobs", "enqueue", str(project)])
    result = runner.invoke(cli.app, ["worker", "run", "--once", "--max-attempts", "1"])
    assert result.exit_code == 1, result.output
    assert "failed" in runner.invoke(cli.app, ["jobs", "list"]).output


@pytest.mark.parametrize("change", ["disappear", "mutate"])
def test_worker_never_acknowledges_missing_or_changing_source(project, monkeypatch, change):
    async def ingest(paths, **options):
        if change == "disappear":
            project.unlink()
            processed = []
        else:
            project.write_text("changed during parsing")
            processed = paths
        return SimpleNamespace(processed=processed, skipped=[], failed={}, wiki=None)

    monkeypatch.setattr(cli.common, "run_checks", lambda *a, **k: [])
    monkeypatch.setattr(
        cli.common,
        "build_app",
        lambda *a: SimpleNamespace(pipeline=SimpleNamespace(ingest=ingest), close=lambda: None),
    )
    runner.invoke(cli.app, ["jobs", "enqueue", str(project)])
    result = runner.invoke(cli.app, ["worker", "run", "--once", "--max-attempts", "1"])
    assert result.exit_code == 1, result.output
    assert "failed" in runner.invoke(cli.app, ["jobs", "list"]).output


def test_worker_handles_cancellation_and_restores_sigterm(project, monkeypatch):
    import asyncio
    import signal

    previous = signal.getsignal(signal.SIGTERM)
    observed = []

    async def ingest(paths, **options):
        observed.append(signal.getsignal(signal.SIGTERM))
        raise asyncio.CancelledError

    monkeypatch.setattr(cli.common, "run_checks", lambda *a, **k: [])
    monkeypatch.setattr(
        cli.common,
        "build_app",
        lambda *a: SimpleNamespace(pipeline=SimpleNamespace(ingest=ingest), close=lambda: None),
    )
    runner.invoke(cli.app, ["jobs", "enqueue", str(project)])
    result = runner.invoke(cli.app, ["worker", "run", "--once"])
    assert "Worker остановлен" in result.output
    assert observed == [signal.default_int_handler]
    assert signal.getsignal(signal.SIGTERM) == previous
    assert "running" in runner.invoke(cli.app, ["jobs", "list"]).output


def test_worker_rebinds_changed_queued_revision_before_ingest(project, monkeypatch):
    from mnogobase.background.queue import JobQueue
    from mnogobase.core.ids import file_doc_id

    calls = []

    async def ingest(paths, **options):
        calls.extend(paths)
        return SimpleNamespace(processed=paths, skipped=[], failed={}, wiki=None)

    monkeypatch.setattr(cli.common, "run_checks", lambda *a, **k: [])
    monkeypatch.setattr(
        cli.common,
        "build_app",
        lambda *a: SimpleNamespace(pipeline=SimpleNamespace(ingest=ingest), close=lambda: None),
    )
    runner.invoke(cli.app, ["jobs", "enqueue", str(project)])
    project.write_text("version B")
    result = runner.invoke(cli.app, ["worker", "run", "--once"])
    assert result.exit_code == 1, result.output
    assert calls == []
    q = JobQueue(database_target(Settings()))
    assert q.counts() == {"pending": 1, "superseded": 1}
    project.write_text("document")
    q.enqueue(project, file_doc_id(project))
    assert q.claim().fingerprint == file_doc_id(project)
    q.close()


@pytest.mark.parametrize("arguments", [["list"], ["retry"], ["retry", "--id", "1"]])
def test_empty_queue_commands_do_not_create_a_database(project, monkeypatch, arguments):
    monkeypatch.setattr(cli.common, "build_app", lambda *a: pytest.fail("models constructed"))

    result = runner.invoke(cli.app, ["jobs", *arguments])

    assert result.exit_code == 0, result.output
    assert "Очередь пуста" in result.output
    assert not (project.parent / ".mnogobase/jobs.db").exists()


def test_enqueue_rejects_unsupported_sources_without_creating_queue(project):
    unsupported = project.with_suffix(".exe")
    unsupported.write_text("unsupported")

    result = runner.invoke(cli.app, ["jobs", "enqueue", str(unsupported)])

    assert result.exit_code == 2, result.output
    assert "Подходящих файлов не найдено" in result.output
    assert not (project.parent / ".mnogobase/jobs.db").exists()


def test_enqueue_closes_queue_if_discovered_source_disappears(project, monkeypatch, tracked_queues):
    def removed_before_hash(path):
        path.unlink()
        return file_doc_id(path)

    monkeypatch.setattr(background_cli, "file_doc_id", removed_before_hash)

    result = runner.invoke(cli.app, ["jobs", "enqueue", str(project)])

    assert result.exit_code == 2, result.output
    assert not project.exists()
    assert len(tracked_queues) == 1 and tracked_queues[0].closed
    q = JobQueue(database_target(Settings()))
    try:
        assert q.jobs() == []
    finally:
        q.close()


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        (["--once", "--watch", "."], "--watch работает непрерывно"),
        (["--watch", "missing"], "существующим каталогом"),
        (["--watch", "a.md"], "существующим каталогом"),
        (["--interval", "0"], "0.1"),
        (["--settle", "-1"], "0"),
        (["--max-attempts", "0"], "1"),
        (["--retry-delay", "0"], "0.1"),
    ],
)
def test_worker_rejects_invalid_options_before_opening_queue(
    project, monkeypatch, arguments, error
):
    monkeypatch.setattr(cli.common, "build_app", lambda *a: pytest.fail("models constructed"))

    result = runner.invoke(cli.app, ["worker", "run", *arguments])

    assert result.exit_code == 2, result.output
    assert error in result.output
    assert not (project.parent / ".mnogobase/jobs.db").exists()


def test_job_progress_persists_steps_and_counts_and_updates_display(tmp_path):
    q, observer = (
        JobQueue(database_target(Settings())),
        JobQueue(database_target(Settings())),
    )
    try:
        job_id = q.enqueue(tmp_path / "a.md", "revision")
        q.claim()
        bars = Progress(disable=True)
        progress = background_cli.JobProgress(q, job_id, cli.render.RichProgress(bars))
        progress.files_found(2)
        progress.file_started("/documents/a.md")

        progress.step("embed", 4)
        assert observer.jobs()[0].progress == "embed 0/4; ошибок 0"
        progress.advance()
        assert observer.jobs()[0].progress == "embed 1/4; ошибок 0"
        progress.advance(2, failed=True)
        assert observer.jobs()[0].progress == "embed 3/4; ошибок 2"
        assert bars.tasks[1].description == "a.md · embed"
        assert (bars.tasks[1].total, bars.tasks[1].completed) == (4, 3)
        assert "ошибок: 2" in bars.tasks[1].fields["note"]

        progress.file_done("processed")
        assert len(bars.tasks) == 1
        assert (bars.tasks[0].total, bars.tasks[0].completed) == (2, 1)
        assert bars.tasks[0].fields["note"] == "1 processed"
        progress.step("wiki")
        assert observer.jobs()[0].progress == "wiki; ошибок 0"
        progress.advance(failed=True)
        assert observer.jobs()[0].progress == "wiki; ошибок 1"
        assert bars.tasks[1].description == "wiki"
        assert bars.tasks[1].total is None
    finally:
        observer.close()
        q.close()


def test_watch_loop_scans_new_files_and_excludes_project_outputs(
    project, monkeypatch, tracked_queues
):
    settings = cli.common.load_settings()
    for directory in background_cli._excluded(settings):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "generated.md").write_text("generated content")
    db = database_target(settings)
    first, second, stop = threading.Event(), threading.Event(), threading.Event()
    other = project.with_name("b.md")
    queue_type = background_cli.JobQueue
    health = []

    class NotifyingQueue(queue_type):
        def enqueue(self, path, fingerprint, *, force=False):
            job_id = super().enqueue(path, fingerprint, force=force)
            (second if path == other else first).set()
            return job_id

    monkeypatch.setattr(background_cli, "JobQueue", NotifyingQueue)
    thread = threading.Thread(
        target=background_cli._watch_loop,
        args=(db, settings, [project.parent], stop, 0.01, 0, health),
    )
    thread.start()
    try:
        assert first.wait(5), "watcher did not enqueue the initial source"
        other.write_text("second document")
        assert second.wait(5), "watcher did not scan again for a new source"
    finally:
        stop.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert health == []
    assert len(tracked_queues) == 1 and tracked_queues[0].closed
    q = JobQueue(database_target(Settings()))
    try:
        assert {job.path for job in q.jobs()} == {str(project), str(other)}
        assert q.counts() == {"pending": 2}
    finally:
        q.close()


def test_watch_loop_reports_failure_to_open_queue(project):
    db = DatabaseTarget("postgresql://test:test@127.0.0.1:1/unavailable", "test", timeout=1)
    health = []

    background_cli._watch_loop(
        db, cli.common.load_settings(), [project.parent], threading.Event(), 0.1, 0, health
    )

    assert health == ["DatabaseError"]


def test_worker_exits_on_watcher_database_failure_and_releases_resources(
    project, monkeypatch, tracked_queues
):
    queue_type = background_cli.JobQueue
    previous_term = signal.getsignal(signal.SIGTERM)
    watcher_failed = threading.Event()
    process_one = background_cli.process_one
    dispatches = 0

    def open_queue(path):
        q = queue_type(path)
        if q.owner == "mnogobase-watch":
            q._db.execute("SET default_transaction_read_only = on")
            q.before_close = watcher_failed.set
        return q

    async def bounded_process_one(*args, **kwargs):
        nonlocal dispatches
        dispatches += 1
        assert dispatches == 1, "worker kept dispatching after the watcher failed"
        assert watcher_failed.wait(5), "watcher did not report its database failure"
        return await process_one(*args, **kwargs)

    monkeypatch.setattr(background_cli, "JobQueue", open_queue)
    monkeypatch.setattr(background_cli, "process_one", bounded_process_one)
    monkeypatch.setattr(cli.common, "build_app", lambda *a: pytest.fail("models constructed"))

    result = runner.invoke(
        cli.app,
        ["worker", "run", "--watch", str(project.parent), "--settle", "0", "--interval", "0.1"],
    )

    assert result.exit_code == 2, result.output
    assert "Watcher остановился: InternalError" in result.output
    assert {q.owner for q in tracked_queues} == {"MainThread", "mnogobase-watch"}
    assert all(q.closed for q in tracked_queues)
    assert signal.getsignal(signal.SIGTERM) == previous_term
    assert not any(t.name == "mnogobase-watch" for t in threading.enumerate())
    with database_lock(Settings(), "worker"):
        pass
    q = JobQueue(database_target(Settings()))
    try:
        assert q.jobs() == []
    finally:
        q.close()


def test_worker_watch_joins_watcher_before_closing_app(project, monkeypatch, tracked_queues):
    closing, release, finished = threading.Event(), threading.Event(), threading.Event()
    abort = threading.Event()
    stops, watcher_threads, results, ingested, app_closed = [], [], [], [], []
    watch_loop = background_cli._watch_loop
    process_one = background_cli.process_one

    async def cancellable_process_one(*args, **kwargs):
        if abort.is_set():
            raise asyncio.CancelledError
        return await process_one(*args, **kwargs)

    def observe_watch(db, settings, roots, stop, interval, settle, health):
        stops.append(stop)
        watcher_threads.append(threading.current_thread())
        watch_loop(db, settings, roots, stop, interval, settle, health)

    def wait_for_release():
        closing.set()
        assert release.wait(5), "test did not release watcher cleanup"

    async def ingest(paths, **options):
        ingested.extend(paths)
        watcher = next(q for q in tracked_queues if q.owner == "mnogobase-watch")
        watcher.before_close = wait_for_release
        raise asyncio.CancelledError

    def close_app():
        assert all(q.closed for q in tracked_queues if q.owner == "mnogobase-watch")
        assert all(not thread.is_alive() for thread in watcher_threads)
        app_closed.append(True)

    def invoke():
        try:
            results.append(
                runner.invoke(
                    cli.app,
                    [
                        "worker",
                        "run",
                        "--watch",
                        str(project.parent),
                        "--settle",
                        "0",
                        "--interval",
                        "0.1",
                    ],
                )
            )
        finally:
            finished.set()

    monkeypatch.setattr(background_cli, "_watch_loop", observe_watch)
    monkeypatch.setattr(background_cli, "process_one", cancellable_process_one)
    monkeypatch.setattr(cli.common, "run_checks", lambda *a, **k: [])
    monkeypatch.setattr(
        cli.common,
        "build_app",
        lambda *a: SimpleNamespace(pipeline=SimpleNamespace(ingest=ingest), close=close_app),
    )
    command = threading.Thread(target=invoke, name="test-worker-command")
    command.start()
    try:
        assert closing.wait(5), "watcher cleanup did not begin after cancellation"
        assert not finished.wait(0.05), "worker returned before watcher cleanup finished"
    finally:
        abort.set()
        for stop in stops:
            stop.set()
        release.set()
        command.join(timeout=5)
        for thread in watcher_threads:
            thread.join(timeout=5)

    assert not command.is_alive()
    assert len(results) == 1
    assert results[0].exit_code == 0, results[0].output
    assert "Worker остановлен" in results[0].output
    assert ingested == [project]
    assert app_closed == [True]
    assert len(tracked_queues) == 2 and all(q.closed for q in tracked_queues)
    with (
        database_lock(Settings(), "worker"),
        database_lock(Settings(), "ingest"),
    ):
        pass
    q = JobQueue(database_target(Settings()))
    try:
        job = q.jobs()[0]
        assert (job.status, job.attempts, job.fingerprint) == ("running", 1, file_doc_id(project))
    finally:
        q.close()


def test_worker_missing_source_fails_job_before_constructing_models(project, monkeypatch):
    monkeypatch.setattr(cli.common, "build_app", lambda *a: pytest.fail("models constructed"))
    monkeypatch.setattr(cli.common, "preflight", lambda *a: pytest.fail("services checked"))
    assert runner.invoke(cli.app, ["jobs", "enqueue", str(project)]).exit_code == 0
    project.unlink()

    result = runner.invoke(cli.app, ["worker", "run", "--once", "--max-attempts", "1"])

    assert result.exit_code == 1, result.output
    q = JobQueue(database_target(Settings()))
    try:
        job = q.jobs()[0]
        assert (job.status, job.attempts, job.error) == ("failed", 1, "FileNotFoundError")
        assert job.progress == "попытка не удалась"
    finally:
        q.close()


def _lock_holder_pid(key: str) -> int | None:
    """pid сессии, которая держит advisory lock `key` проекта."""
    from mnogobase.core.database import _lock_key, connect

    target = database_target(Settings())
    value = _lock_key(target, key) & 0xFFFFFFFFFFFFFFFF
    db = connect(target)
    try:
        row = db.execute(
            "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted "
            "AND classid::bigint = ? AND objid::bigint = ? AND objsubid = 1",
            (value >> 32, value & 0xFFFFFFFF),
        ).fetchone()
        return row[0] if row else None
    finally:
        db.close()


@pytest.fixture
def fast_retries(monkeypatch):
    monkeypatch.setattr(background_cli, "DB_RETRY_MIN_S", 0.01, raising=False)
    monkeypatch.setattr(background_cli, "DB_RETRY_MAX_S", 0.02, raising=False)


def _outage_queue(monkeypatch, failures, before_failure=None):
    """JobQueue, у которой `heartbeat` первые `failures` раз видит «PostgreSQL недоступен»."""
    from mnogobase.core.database import DatabaseError

    queue_type = background_cli.JobQueue
    state = {"left": failures}

    class OutageQueue(queue_type):
        def heartbeat(self):
            if threading.current_thread() is threading.main_thread() and state["left"]:
                state["left"] -= 1
                if before_failure is not None:
                    before_failure()
                raise DatabaseError("PostgreSQL недоступен: проверьте настройки базы и сервер")
            super().heartbeat()

    monkeypatch.setattr(background_cli, "JobQueue", OutageQueue)
    return state


def _stop_after_first_job(monkeypatch):
    process_one = background_cli.process_one

    async def stopping_process_one(*args, **kwargs):
        if await process_one(*args, **kwargs):
            raise KeyboardInterrupt
        return False

    monkeypatch.setattr(background_cli, "process_one", stopping_process_one)


def test_worker_waits_out_database_outage_and_requeues_orphaned_job(
    project, monkeypatch, fast_retries
):
    calls = []

    async def ingest(paths, **options):
        calls.extend(paths)
        return SimpleNamespace(failed={}, wiki=None, processed=paths, skipped=[])

    def orphan_job():
        # обрыв во время release: задача осталась running, хотя её никто не выполняет
        q = JobQueue(database_target(Settings()))
        try:
            q.claim(5)
        finally:
            q.close()

    monkeypatch.setattr(cli.common, "run_checks", lambda *a, **k: [])
    monkeypatch.setattr(
        cli.common,
        "build_app",
        lambda *a: SimpleNamespace(pipeline=SimpleNamespace(ingest=ingest), close=lambda: None),
    )
    assert runner.invoke(cli.app, ["jobs", "enqueue", str(project)]).exit_code == 0
    state = {"orphaned": False}

    def first_failure_orphans():
        if not state["orphaned"]:
            state["orphaned"] = True
            orphan_job()

    outage = _outage_queue(monkeypatch, 3, first_failure_orphans)
    _stop_after_first_job(monkeypatch)

    result = runner.invoke(cli.app, ["worker", "run", "--interval", "0.1"])

    assert outage["left"] == 0
    assert result.output.count("PostgreSQL недоступен (DatabaseError); повтор через") == 3
    assert "Связь с PostgreSQL восстановлена; возвращено задач в очередь: 1" in result.output
    assert calls == [project]
    jobs = JobQueue(database_target(Settings()))
    try:
        assert [job.status for job in jobs.jobs()] == ["done"]
    finally:
        jobs.close()
    with database_lock(Settings(), "worker"):
        pass


def test_worker_exits_if_its_lock_was_dropped_during_outage(project, monkeypatch, fast_retries):
    from mnogobase.core.database import connect

    def drop_worker_session():
        pid = _lock_holder_pid("worker")
        assert pid is not None
        db = connect(database_target(Settings()))
        try:
            db.execute("SELECT pg_terminate_backend(?)", (pid,))
        finally:
            db.close()

    monkeypatch.setattr(cli.common, "build_app", lambda *a: pytest.fail("models constructed"))
    _outage_queue(monkeypatch, 1, drop_worker_session)

    result = runner.invoke(cli.app, ["worker", "run", "--interval", "0.1"])

    assert result.exit_code == 2, result.output
    assert "потерял блокировку проекта" in result.output
    assert _lock_holder_pid("worker") is None
    with database_lock(Settings(), "worker"):
        pass


def test_worker_once_still_exits_on_database_outage(project, monkeypatch, fast_retries):
    monkeypatch.setattr(cli.common, "build_app", lambda *a: pytest.fail("models constructed"))
    _outage_queue(monkeypatch, 1)

    result = runner.invoke(cli.app, ["worker", "run", "--once"])

    assert result.exit_code == 2, result.output
    assert "PostgreSQL недоступен: проверьте настройки базы и сервер" in result.output
    assert "повтор через" not in result.output


def test_watch_loop_waits_out_database_outage(project, monkeypatch, fast_retries):
    from mnogobase.core.database import DatabaseError

    settings = cli.common.load_settings()
    watcher_type = background_cli.Watcher
    scans, enqueued, stop, health = [], threading.Event(), threading.Event(), []

    class FlakyWatcher(watcher_type):
        def scan(self):
            scans.append(1)
            if len(scans) <= 2:
                raise DatabaseError("PostgreSQL недоступен: проверьте настройки базы и сервер")
            super().scan()
            if self.queue.jobs():
                enqueued.set()

    monkeypatch.setattr(background_cli, "Watcher", FlakyWatcher)
    thread = threading.Thread(
        target=background_cli._watch_loop,
        args=(database_target(settings), settings, [project.parent], stop, 0.05, 0, health),
    )
    thread.start()
    try:
        assert enqueued.wait(10), "watcher did not recover after the outage"
    finally:
        stop.set()
        thread.join(10)
    assert health == []
    assert len(scans) >= 3
