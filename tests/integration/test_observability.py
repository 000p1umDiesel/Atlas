from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal

import pytest

from mnogobase.background.queue import JobQueue
from mnogobase.config import LLMRate
from mnogobase.database import connect, database_target
from mnogobase.llm.telemetry import CallEvent
from mnogobase.service.context import team_settings
from mnogobase.service.monitor import ServiceMonitor
from mnogobase.service.observability import ObservabilityStore
from mnogobase.service.store import ServiceStore

pytestmark = pytest.mark.integration


@pytest.fixture
def setup(pg_settings):
    service = ServiceStore(database_target(pg_settings, "service"))
    teams = [service.create_team(name)["id"] for name in ("A", "B")]
    store = ObservabilityStore(database_target(pg_settings, "service"))
    return pg_settings, teams, store


def test_usage_idempotency_rates_unknown_and_restart(setup):
    settings, (a, b), store = setup
    rate = LLMRate(currency="USD", input_per_million="1.25", output_per_million="2.5")
    e = CallEvent(
        "one", a, "m", "answer", 100, tokens_in=1000000, tokens_out=2000000, status="response"
    )
    store.record_usage(e, rate)
    store.record_usage(e, LLMRate(currency="EUR", input_per_million=99, output_per_million=99))
    store.record_usage(replace(e, id="unknown", tokens_in=None), rate)
    store.record_usage(replace(e, id="other", team_id=b), None)
    store.record_usage(replace(e, id="local", model="local", external=False), rate)
    fresh = ObservabilityStore(store.target)
    rows = fresh.usage(team_id=a, since=100, until=101)
    m = next(r for r in rows if r["model"] == "m")
    assert m["calls"] == 2 and m["unknown_usage"] == 1
    assert Decimal(m["estimated_cost"]) == Decimal("6.25") and m["currency"] == "USD"
    assert m["unpriced_calls"] == 1
    assert len(fresh.usage(team_id=b)) == 1
    assert fresh.usage(since=101) == []
    local = next(r for r in rows if r["model"] == "local")
    assert local["estimated_cost"] is None


def test_concurrent_recorders_deduplicate_same_attempt(setup):
    settings, (a, _), store = setup
    e = CallEvent("same", a, "m", "query", 100, status="response", tokens_in=3, tokens_out=5)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: store.record_usage(e), range(12)))
    assert store.usage(team_id=a)[0]["calls"] == 1
    store.record_usage(replace(e, status="started", tokens_in=None, tokens_out=None))
    assert store.usage(team_id=a)[0]["tokens_in"] == 3


def test_monitor_detection_resolution_recurrence_and_no_retry(setup, tmp_path):
    settings, (a, b), store = setup
    now = [100.0]
    scoped = team_settings(settings, a)
    q = JobQueue(database_target(scoped, "jobs"), clock=lambda: now[0])
    try:
        job_id = q.enqueue(tmp_path / "doc.md", "hash")
        monitor = ServiceMonitor(settings, clock=lambda: now[0])
        monitor.scan()  # starts the initial grace period
        assert store.alerts() == []
        now[0] = 1101
        monitor.scan()
        assert {r["kind"] for r in store.alerts()} == {"worker_stale", "job_pending"}
        monitor.scan()
        assert len(store.alerts()) == 2
        assert store.alerts(team_id=b) == []
        store.heartbeat(now[0])
        job = q.claim()
        assert job.id == job_id and job.started_at == now[0]
        monitor.scan()
        assert store.alerts() == []
        now[0] += 3601
        store.heartbeat(now[0])
        monitor.scan()
        assert store.alerts()[0]["kind"] == "job_running"
        assert q.get(job_id).status == "running"
        q.fail(job_id, "private exception", max_attempts=1, delay=0)
        monitor.scan()
        assert store.alerts()[0]["kind"] == "job_failed"
        assert "private exception" not in str(store.alerts(history=True))
        q.retry(job_id)
        monitor.scan()
        assert store.alerts() == []
        q.claim()
        q.fail(job_id, "secret", max_attempts=1, delay=0)
        monitor.scan()
        assert len([r for r in store.alerts(history=True) if r["kind"] == "job_failed"]) == 2
    finally:
        q.close()


def test_existing_queue_migrates_preserving_jobs(setup, tmp_path):
    settings, (a, _), _ = setup
    target = database_target(team_settings(settings, a), "jobs")
    q = JobQueue(target)
    jid = q.enqueue(tmp_path / "old.md", "old")
    q.close()
    db = connect(target)
    try:
        for col in ("created_at", "updated_at", "started_at"):
            db.execute(f"ALTER TABLE jobs DROP COLUMN {col}")
    finally:
        db.close()
    q = JobQueue(target, clock=lambda: 100)
    try:
        assert q.get(jid).fingerprint == "old"
        assert q.get(jid).created_at == 100
    finally:
        q.close()


def test_operator_cli_reports_usage_alerts_and_validates_period(setup, monkeypatch):
    import json

    import typer
    from typer.testing import CliRunner

    from mnogobase.service.cli import register_cli

    settings, (a, b), store = setup
    monkeypatch.setattr("mnogobase.service.cli._settings", lambda: settings)
    app = typer.Typer()
    register_cli(app)
    runner = CliRunner()
    store.record_usage(
        CallEvent("cli", a, "m", "answer", 100, status="response", tokens_in=3, tokens_out=5)
    )
    result = runner.invoke(
        app, ["service", "usage", "--team", a, "--since", "1970-01-01T00:01:40Z"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["tokens_in"] == 3
    assert json.loads(runner.invoke(app, ["service", "usage", "--team", b]).output) == []
    assert runner.invoke(app, ["service", "usage", "--since", "bad"]).exit_code == 2
    assert (
        runner.invoke(
            app, ["service", "usage", "--since", "2026-02-01", "--until", "2026-01-01"]
        ).exit_code
        == 2
    )
    assert runner.invoke(app, ["service", "monitor", "--once"]).exit_code == 0
    result = runner.invoke(app, ["service", "alerts", "--history"])
    assert result.exit_code == 0 and json.loads(result.output) == []


async def test_runtime_records_team_and_job_context(setup, tmp_path):
    from types import SimpleNamespace

    import httpx

    from mnogobase.config import LLMSettings
    from mnogobase.llm.client import OpenAICompatLLM
    from mnogobase.service.runtime import TeamService
    from tests.unit.test_llm_client import _completion

    settings, (a, b), store = setup
    settings.service.root = tmp_path / "service"
    settings.service.monitor.heartbeat_s = 0.01

    def factory(scoped):
        llm = OpenAICompatLLM(
            LLMSettings(model="m"),
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_completion("ok")))
            ),
        )

        async def ingest(*args, **kwargs):
            await llm.complete([], task="extract")
            return SimpleNamespace(failed={}, wiki=None)

        return SimpleNamespace(llm=llm, pipeline=SimpleNamespace(ingest=ingest), aclose=llm.aclose)

    runtime = TeamService(settings, app_factory=factory)
    user = runtime.store.create_user("operator", "password")
    for team in (a, b):
        runtime.store.add_member(team, user["id"], "admin")
        ctx = runtime.store.context(user["id"], team)
        runtime.upload(ctx, "doc.md", b"document")
    assert await runtime.work_once() == 2
    for team in (a, b):
        assert store.usage(team_id=team)[0]["tokens_in"] == 3
        async with runtime._app(runtime._settings(team)) as app:
            await app.llm.complete([], task="answer")
    db = connect(store.target)
    try:
        assert (
            db.execute("SELECT COUNT(*) FROM llm_usage WHERE job_id IS NOT NULL").fetchone()[0] == 2
        )
    finally:
        db.close()


def test_monitor_does_not_wait_for_active_queue_transaction(setup, tmp_path):
    settings, (a, _), store = setup
    target = database_target(team_settings(settings, a), "jobs")
    q = JobQueue(target)
    jid = q.enqueue(tmp_path / "doc.md", "hash")
    q._db.execute("BEGIN")
    q._db.execute("UPDATE jobs SET progress=? WHERE id=?", ("active", jid))
    try:
        # A modern queue needs no ALTER lock; scan must complete before rollback.
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(ServiceMonitor(settings).scan)
            assert future.result(timeout=3)["active_conditions"] == 0
    finally:
        q._db.execute("ROLLBACK")
        q.close()


def test_worker_cli_rejects_poll_interval_that_would_cause_false_stale_alert(setup, monkeypatch):
    import typer
    from typer.testing import CliRunner

    from mnogobase.service.cli import register_cli

    settings, _, _ = setup
    monkeypatch.setattr("mnogobase.service.cli._settings", lambda: settings)
    app = typer.Typer()
    register_cli(app)
    result = CliRunner().invoke(app, ["service", "work", "--interval", "300"])
    assert result.exit_code == 2 and "Worker interval" in result.output


def test_imported_legacy_rows_receive_timestamp_defaults(setup, tmp_path):
    settings, (a, _), _ = setup
    q = JobQueue(database_target(team_settings(settings, a), "jobs"))
    try:
        # The historical SQLite importer inserts only columns present in its source.
        q._db.execute(
            "INSERT INTO jobs(path,fingerprint) VALUES(?,?)", (str(tmp_path / "legacy"), "old")
        )
        row = q.jobs()[0]
        assert row.created_at is not None and row.updated_at is not None
        ServiceMonitor(settings).scan()
    finally:
        q.close()
