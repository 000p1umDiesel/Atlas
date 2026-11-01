import asyncio
import signal
import sqlite3
import threading
from types import SimpleNamespace

import pytest
from filelock import FileLock
from rich.progress import Progress
from typer.testing import CliRunner

from mnogobase import cli
from mnogobase.background import cli as background_cli
from mnogobase.background.queue import JobQueue
from mnogobase.ids import file_doc_id

runner = CliRunner()


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "configure_logging", lambda *a: None)
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
            with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
                self.counts()
            self.closed = True

    monkeypatch.setattr(background_cli, "JobQueue", TrackedQueue)
    return queues


def test_enqueue_list_and_retry_without_models(project, monkeypatch):
    monkeypatch.setattr(cli, "build_app", lambda *a: pytest.fail("models constructed"))
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
        observer = JobQueue(project.parent / ".mnogobase/jobs.db")
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
    monkeypatch.setattr(cli, "_preflight", lambda *a: None)
    monkeypatch.setattr(cli, "build_app", lambda *a: app)
    result = runner.invoke(cli.app, ["jobs", "enqueue", str(project)])
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli.app, ["worker", "run", "--once"])
    assert result.exit_code == 0, result.output
    assert calls == [project] and closed == [1]
    assert snapshots == ["parse 0/1; failed 0", "parse 1/1; failed 0"]
    result = runner.invoke(cli.app, ["jobs", "list"])
    assert "done" in result.output


def test_second_worker_and_busy_writer_leave_job_unclaimed(project):
    runner.invoke(cli.app, ["jobs", "enqueue", str(project)])
    with FileLock(".mnogobase/worker.lock", timeout=0):
        result = runner.invoke(cli.app, ["worker", "run", "--once"])
    assert result.exit_code == 2
    with FileLock(".mnogobase/ingest.lock", timeout=0):
        result = runner.invoke(cli.app, ["worker", "run", "--once"])
    assert result.exit_code == 1, result.output
    from mnogobase.background.queue import JobQueue

    q = JobQueue(project.parent / ".mnogobase/jobs.db")
    assert q.jobs()[0].attempts == 0
    q.close()


def test_worker_wiki_failure_remains_retryable(project, monkeypatch):
    async def ingest(paths, **options):
        return SimpleNamespace(failed={}, wiki=SimpleNamespace(failed=["page"]))

    monkeypatch.setattr(cli, "_preflight", lambda *a: None)
    monkeypatch.setattr(
        cli,
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

    monkeypatch.setattr(cli, "_preflight", lambda *a: None)
    monkeypatch.setattr(
        cli,
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

    monkeypatch.setattr(cli, "_preflight", lambda *a: None)
    monkeypatch.setattr(
        cli,
        "build_app",
        lambda *a: SimpleNamespace(pipeline=SimpleNamespace(ingest=ingest), close=lambda: None),
    )
    runner.invoke(cli.app, ["jobs", "enqueue", str(project)])
    result = runner.invoke(cli.app, ["worker", "run", "--once"])
    assert "Worker stopped" in result.output
    assert observed == [signal.default_int_handler]
    assert signal.getsignal(signal.SIGTERM) == previous
    assert "running" in runner.invoke(cli.app, ["jobs", "list"]).output


def test_worker_rebinds_changed_queued_revision_before_ingest(project, monkeypatch):
    from mnogobase.background.queue import JobQueue
    from mnogobase.ids import file_doc_id

    calls = []

    async def ingest(paths, **options):
        calls.extend(paths)
        return SimpleNamespace(processed=paths, skipped=[], failed={}, wiki=None)

    monkeypatch.setattr(cli, "_preflight", lambda *a: None)
    monkeypatch.setattr(
        cli,
        "build_app",
        lambda *a: SimpleNamespace(pipeline=SimpleNamespace(ingest=ingest), close=lambda: None),
    )
    runner.invoke(cli.app, ["jobs", "enqueue", str(project)])
    project.write_text("version B")
    result = runner.invoke(cli.app, ["worker", "run", "--once"])
    assert result.exit_code == 1, result.output
    assert calls == []
    q = JobQueue(project.parent / ".mnogobase/jobs.db")
    assert q.counts() == {"pending": 1, "superseded": 1}
    project.write_text("document")
    q.enqueue(project, file_doc_id(project))
    assert q.claim().fingerprint == file_doc_id(project)
    q.close()


@pytest.mark.parametrize("arguments", [["list"], ["retry"], ["retry", "--id", "1"]])
def test_empty_queue_commands_do_not_create_a_database(project, monkeypatch, arguments):
    monkeypatch.setattr(cli, "build_app", lambda *a: pytest.fail("models constructed"))

    result = runner.invoke(cli.app, ["jobs", *arguments])

    assert result.exit_code == 0, result.output
    assert "Queue is empty" in result.output
    assert not (project.parent / ".mnogobase/jobs.db").exists()


def test_enqueue_rejects_unsupported_sources_without_creating_queue(project):
    unsupported = project.with_suffix(".exe")
    unsupported.write_text("unsupported")

    result = runner.invoke(cli.app, ["jobs", "enqueue", str(unsupported)])

    assert result.exit_code == 2, result.output
    assert "No supported source files" in result.output
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
    q = JobQueue(project.parent / ".mnogobase/jobs.db")
    try:
        assert q.jobs() == []
    finally:
        q.close()


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        (["--once", "--watch", "."], "--watch is continuous"),
        (["--watch", "missing"], "existing directory"),
        (["--watch", "a.md"], "existing directory"),
        (["--interval", "0"], "0.1"),
        (["--settle", "-1"], "0"),
        (["--max-attempts", "0"], "1"),
        (["--retry-delay", "0"], "0.1"),
    ],
)
def test_worker_rejects_invalid_options_before_opening_queue(
    project, monkeypatch, arguments, error
):
    monkeypatch.setattr(cli, "build_app", lambda *a: pytest.fail("models constructed"))

    result = runner.invoke(cli.app, ["worker", "run", *arguments])

    assert result.exit_code == 2, result.output
    assert error in result.output
    assert not (project.parent / ".mnogobase/jobs.db").exists()


def test_job_progress_persists_steps_and_counts_and_updates_display(tmp_path):
    db = tmp_path / "jobs.db"
    q, observer = JobQueue(db), JobQueue(db)
    try:
        job_id = q.enqueue(tmp_path / "a.md", "revision")
        q.claim()
        bars = Progress(disable=True)
        progress = background_cli.JobProgress(q, job_id, cli.RichProgress(bars))
        progress.files_found(2)
        progress.file_started("/documents/a.md")

        progress.step("embed", 4)
        assert observer.jobs()[0].progress == "embed 0/4; failed 0"
        progress.advance()
        assert observer.jobs()[0].progress == "embed 1/4; failed 0"
        progress.advance(2, failed=True)
        assert observer.jobs()[0].progress == "embed 3/4; failed 2"
        assert bars.tasks[1].description == "a.md · embed"
        assert (bars.tasks[1].total, bars.tasks[1].completed) == (4, 3)
        assert "2 failed" in bars.tasks[1].fields["note"]

        progress.file_done("processed")
        assert len(bars.tasks) == 1
        assert (bars.tasks[0].total, bars.tasks[0].completed) == (2, 1)
        assert bars.tasks[0].fields["note"] == "1 processed"
        progress.step("wiki")
        assert observer.jobs()[0].progress == "wiki; failed 0"
        progress.advance(failed=True)
        assert observer.jobs()[0].progress == "wiki; failed 1"
        assert bars.tasks[1].description == "wiki"
        assert bars.tasks[1].total is None
    finally:
        observer.close()
        q.close()


def test_watch_loop_scans_new_files_and_excludes_project_outputs(
    project, monkeypatch, tracked_queues
):
    settings = cli.load_settings()
    for directory in background_cli._excluded(settings):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "generated.md").write_text("generated content")
    db = settings.data_dir / "jobs.db"
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
    q = JobQueue(db)
    try:
        assert {job.path for job in q.jobs()} == {str(project), str(other)}
        assert q.counts() == {"pending": 2}
    finally:
        q.close()


def test_watch_loop_reports_failure_to_open_queue(project):
    db = project.parent / "directory-not-database"
    db.mkdir()
    health = []

    background_cli._watch_loop(
        db, cli.load_settings(), [project.parent], threading.Event(), 0.1, 0, health
    )

    assert health == ["OperationalError"]


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
            q._db.execute("PRAGMA query_only=ON")
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
    monkeypatch.setattr(cli, "build_app", lambda *a: pytest.fail("models constructed"))

    result = runner.invoke(
        cli.app,
        ["worker", "run", "--watch", str(project.parent), "--settle", "0", "--interval", "0.1"],
    )

    assert result.exit_code == 2, result.output
    assert "Watcher stopped: OperationalError" in result.output
    assert {q.owner for q in tracked_queues} == {"MainThread", "mnogobase-watch"}
    assert all(q.closed for q in tracked_queues)
    assert signal.getsignal(signal.SIGTERM) == previous_term
    assert not any(t.name == "mnogobase-watch" for t in threading.enumerate())
    with FileLock(".mnogobase/worker.lock", timeout=0):
        pass
    q = JobQueue(project.parent / ".mnogobase/jobs.db")
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
    monkeypatch.setattr(cli, "_preflight", lambda *a: None)
    monkeypatch.setattr(
        cli,
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
    assert "Worker stopped" in results[0].output
    assert ingested == [project]
    assert app_closed == [True]
    assert len(tracked_queues) == 2 and all(q.closed for q in tracked_queues)
    with (
        FileLock(".mnogobase/worker.lock", timeout=0),
        FileLock(".mnogobase/ingest.lock", timeout=0),
    ):
        pass
    q = JobQueue(project.parent / ".mnogobase/jobs.db")
    try:
        job = q.jobs()[0]
        assert (job.status, job.attempts, job.fingerprint) == ("running", 1, file_doc_id(project))
    finally:
        q.close()


def test_worker_missing_source_fails_job_before_constructing_models(project, monkeypatch):
    monkeypatch.setattr(cli, "build_app", lambda *a: pytest.fail("models constructed"))
    monkeypatch.setattr(cli, "_preflight", lambda *a: pytest.fail("services checked"))
    assert runner.invoke(cli.app, ["jobs", "enqueue", str(project)]).exit_code == 0
    project.unlink()

    result = runner.invoke(cli.app, ["worker", "run", "--once", "--max-attempts", "1"])

    assert result.exit_code == 1, result.output
    q = JobQueue(project.parent / ".mnogobase/jobs.db")
    try:
        job = q.jobs()[0]
        assert (job.status, job.attempts, job.error) == ("failed", 1, "FileNotFoundError")
        assert job.progress == "failed attempt"
    finally:
        q.close()
