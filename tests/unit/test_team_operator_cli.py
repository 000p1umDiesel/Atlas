import json

import pytest
import typer
from filelock import FileLock, Timeout
from typer.testing import CliRunner

from mnogobase import cli
from mnogobase.background.queue import JobQueue
from mnogobase.config import Settings
from mnogobase.ids import file_doc_id
from mnogobase.registry import Registry, ingest_lock
from mnogobase.service.context import team_settings
from mnogobase.service.store import ServiceStore


def test_team_legacy_worker_refuses_before_claiming_a_typed_remove(tmp_path, monkeypatch):
    base = Settings(service={"root": tmp_path / "service"})
    team = ServiceStore(base.service.root / "service.db").create_team("Team")
    scoped = team_settings(base, team["id"])
    source = scoped.data_dir.parent / "documents" / "document.md"
    source.parent.mkdir(parents=True)
    source.write_text("Team evidence")
    queue = JobQueue(scoped.data_dir / "jobs.db")
    try:
        queue.enqueue(
            source,
            f"remove:{file_doc_id(source)}",
            kind="remove",
            payload_json=json.dumps({"doc_id": file_doc_id(source)}),
        )
        monkeypatch.setattr(cli, "load_settings", lambda *a: base)
        monkeypatch.setattr(cli, "configure_logging", lambda *a: None)
        monkeypatch.setattr(cli, "_preflight", lambda *a: pytest.fail("preflight must not run"))
        monkeypatch.setattr(cli, "build_app", lambda *a: pytest.fail("models must not start"))
        result = CliRunner().invoke(cli.app, ["--team", team["id"], "worker", "run", "--once"])
        jobs = queue.jobs()
        assert len(jobs) == 1, result.output
        assert jobs[0].kind == "remove" and jobs[0].status == "pending"
        assert jobs[0].attempts == 0
        assert result.exit_code == 2, result.output
        assert "mnogobase service work" in result.output
    finally:
        queue.close()


@pytest.fixture
def operator_settings(tmp_path, monkeypatch):
    settings = team_settings(Settings(service={"root": tmp_path / "service"}), "a" * 32)
    Registry(settings.data_dir / "state.db").close()
    monkeypatch.setattr(cli, "_settings", lambda **kw: settings)
    monkeypatch.setattr(cli, "_preflight", lambda *a, **kw: None)
    return settings


def test_team_writer_holds_and_releases_both_service_and_corpus_locks(operator_settings):
    settings = operator_settings
    held = cli._lock(settings)
    try:
        with pytest.raises(Timeout):
            FileLock(settings.service.root / "worker.lock", timeout=0).acquire()
        with pytest.raises(Timeout):
            ingest_lock(settings.data_dir).acquire()
    finally:
        held.release()
    with FileLock(settings.service.root / "worker.lock", timeout=0), ingest_lock(settings.data_dir):
        pass


def test_team_writer_refuses_while_service_dispatcher_is_active(operator_settings):
    settings = operator_settings
    with (
        FileLock(settings.service.root / "worker.lock", timeout=0),
        pytest.raises(typer.Exit) as error,
    ):
        held = cli._lock(settings)
        held.release()
    assert error.value.exit_code == 2
    with ingest_lock(settings.data_dir):
        pass


def test_busy_team_corpus_does_not_leak_the_global_service_lock(operator_settings):
    settings = operator_settings
    with ingest_lock(settings.data_dir):
        with pytest.raises(typer.Exit):
            cli._lock(settings)
        with FileLock(settings.service.root / "worker.lock", timeout=0):
            pass


def test_legacy_writer_does_not_take_the_service_global_lock(operator_settings):
    settings = operator_settings.model_copy(deep=True)
    settings.neo4j.team_id = None
    with FileLock(settings.service.root / "worker.lock", timeout=0):
        held = cli._lock(settings)
        held.release()


@pytest.mark.parametrize("command", [["ask", "Question"], ["compare", "Question"], ["status"]])
def test_team_reads_refuse_a_partially_updated_corpus(operator_settings, monkeypatch, command):
    monkeypatch.setattr(cli, "build_app", lambda *a: pytest.fail("read during corpus update"))
    with ingest_lock(operator_settings.data_dir):
        result = CliRunner().invoke(cli.app, command)
    assert result.exit_code == 2, (result.output, result.exception)
    assert "in progress" in result.output


def test_team_reads_only_lock_their_corpus_without_blocking_other_teams(operator_settings):
    settings = operator_settings
    with (
        FileLock(settings.service.root / "worker.lock", timeout=0),
        cli._read_lock(settings),
        pytest.raises(Timeout),
    ):
        ingest_lock(settings.data_dir).acquire()
    with ingest_lock(settings.data_dir):
        pass


def test_team_eval_ingest_obeys_service_dispatcher_lock(operator_settings, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "build_app", lambda *a: pytest.fail("parallel model work started"))
    with FileLock(operator_settings.service.root / "worker.lock", timeout=0):
        result = CliRunner().invoke(
            cli.app,
            ["eval", "ingest", "source.md", "--output", str(tmp_path / "evaluation")],
        )
    assert result.exit_code == 2, (result.output, result.exception)
    assert "in progress" in result.output
