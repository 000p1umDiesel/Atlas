from types import SimpleNamespace

import pytest
from filelock import FileLock
from typer.testing import CliRunner

from mnogobase import cli

runner = CliRunner()


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "configure_logging", lambda *a: None)
    source = tmp_path / "a.md"
    source.write_text("document")
    return source


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
    calls, closed = [], []

    async def ingest(paths, **options):
        calls.extend(paths)
        return SimpleNamespace(failed={}, wiki=None, processed=paths, skipped=[])

    app = SimpleNamespace(pipeline=SimpleNamespace(ingest=ingest), close=lambda: closed.append(1))
    monkeypatch.setattr(cli, "_preflight", lambda *a: None)
    monkeypatch.setattr(cli, "build_app", lambda *a: app)
    result = runner.invoke(cli.app, ["jobs", "enqueue", str(project)])
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli.app, ["worker", "run", "--once"])
    assert result.exit_code == 0, result.output
    assert calls == [project] and closed == [1]
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
