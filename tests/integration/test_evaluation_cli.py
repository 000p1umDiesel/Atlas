import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from mnogobase import cli
from mnogobase.core.config import Settings
from mnogobase.core.database import LockBusyError, database_target, mark_project
from mnogobase.core.models import ContextItem
from mnogobase.evaluation import core
from mnogobase.retrieval.answer import Answerer
from mnogobase.stores.registry import Registry
from tests.fakes import FakeLLM

pytestmark = pytest.mark.integration

runner = CliRunner()


@pytest.fixture
def offline(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.common, "configure_logging", lambda *a: None)
    path = tmp_path / "questions.jsonl"
    path.write_text(json.dumps({"id": "q", "question": "Q?", "reference": "A"}) + "\n")
    return path


def test_cli_run_uses_three_modes_and_closes_app(offline, tmp_path, monkeypatch):
    Registry(database_target(Settings())).close()
    mark_project(Settings(data_dir=tmp_path / ".mnogobase"))
    closed = []
    monkeypatch.setattr(cli.common, "preflight", lambda *a: None)
    monkeypatch.setattr(
        cli.common, "build_app", lambda *a: SimpleNamespace(close=lambda: closed.append(True))
    )

    class Retriever:
        def retrieve(self, query, k, alt_queries=()):
            return [ContextItem(kind="chunk", ref="c", text="A")]

    monkeypatch.setattr(
        cli.common,
        "answer_setup",
        lambda app: (
            {m: Retriever() for m in ("rag", "wiki", "graph")},
            Answerer(FakeLLM(lambda *a: "A"), 1000),
            None,
        ),
    )
    result = runner.invoke(cli.app, ["eval", "run", str(offline), "--output", "run"])
    assert result.exit_code == 0, result.output
    rows = core.latest_records(tmp_path / "run/answers.jsonl", ("id", "mode"))
    assert set(rows) == {("q", "rag"), ("q", "wiki"), ("q", "graph")}
    assert closed == [True]
    assert "remaining" in result.output.lower()


def test_cli_report_and_score_do_not_build_app_or_preflight(offline, tmp_path, monkeypatch):
    run = tmp_path / "run"
    core.prepare_run(offline, run, Settings(), modes=["rag"])
    monkeypatch.setattr(cli.common, "build_app", lambda *a: pytest.fail("app was built"))
    monkeypatch.setattr(cli.common, "preflight", lambda *a: pytest.fail("preflight ran"))
    result = runner.invoke(cli.app, ["eval", "report", str(run)])
    assert result.exit_code == 0, result.output
    assert (run / "summary.csv").is_file()
    # Missing inference records are skipped without constructing judge clients.
    result = runner.invoke(cli.app, ["eval", "score", str(run)])
    assert result.exit_code == 1, result.output
    assert "inference_failed_or_missing" in (run / "details.csv").read_text()


def test_cli_invalid_dataset_fails_before_services(offline, tmp_path, monkeypatch):
    offline.write_text('{"id":"x","question":""}\n')
    monkeypatch.setattr(cli.common, "build_app", lambda *a: pytest.fail("app was built"))
    result = runner.invoke(cli.app, ["eval", "run", str(offline), "--output", "run"])
    assert result.exit_code == 2, result.output
    assert "id and question" in result.output


def test_cli_unknown_metric_rejected_without_judge(offline, tmp_path):
    core.prepare_run(offline, tmp_path / "run", Settings())
    result = runner.invoke(cli.app, ["eval", "score", "run", "--metric", "invented"])
    assert result.exit_code == 2, result.output
    assert "metrics" in result.output


def test_eval_ingest_saves_processing_and_closes_app(offline, tmp_path, monkeypatch):
    closed = []

    async def ingest(paths, progress, **kwargs):
        progress.step("parse")
        return SimpleNamespace(processed=paths, skipped=[], failed={}, wiki=None)

    monkeypatch.setattr(cli.common, "preflight", lambda *a: None)
    monkeypatch.setattr(
        cli.common,
        "build_app",
        lambda *a: SimpleNamespace(
            pipeline=SimpleNamespace(ingest=ingest), close=lambda: closed.append(1)
        ),
    )
    result = runner.invoke(
        cli.app, ["eval", "ingest", str(offline), "--phase", "build", "--output", "run"]
    )
    assert result.exit_code == 0, result.output
    rows = core.latest_records(tmp_path / "run/processing.jsonl")
    assert next(iter(rows.values()))["phase"] == "build" and closed == [1]


def test_benchmark_runs_and_resumes_whole_workflow_offline(offline, tmp_path, monkeypatch):
    from mnogobase.stores.registry import ingest_lock

    Registry(database_target(Settings())).close()
    mark_project(Settings(data_dir=tmp_path / ".mnogobase"))
    monkeypatch.setattr(cli.common, "preflight", lambda *a: None)
    monkeypatch.setattr(cli.common, "build_app", lambda *a: SimpleNamespace(close=lambda: None))
    llm = FakeLLM(lambda *a: "Answer [1]")

    class Retriever:
        def retrieve(self, query, k, alt_queries=()):
            with pytest.raises(LockBusyError):
                ingest_lock(database_target(Settings())).acquire()
            return [ContextItem(kind="chunk", ref="c", text="Answer")]

    class Judge:
        def __init__(self, *a):
            pass

        def metrics(self, names):
            class Metric:
                async def ascore(self, **kwargs):
                    return SimpleNamespace(value=1.0)

            return {name: Metric() for name in names}

        async def close(self):
            pass

    monkeypatch.setattr(core, "RagasJudge", Judge)
    monkeypatch.setattr(
        cli.common,
        "answer_setup",
        lambda app: ({m: Retriever() for m in ("rag", "wiki", "graph")}, Answerer(llm, 1000), None),
    )
    args = ["eval", "benchmark", str(offline), "--output", "run"]
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert len(llm.calls) == 3
    assert (tmp_path / "run/paired.csv").is_file()
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert len(llm.calls) == 3
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "changed.md").write_text("changed")
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 2, result.output
    assert "configuration changed" in result.output


def test_benchmark_scores_the_answers_that_succeeded(offline, tmp_path, monkeypatch):
    Registry(database_target(Settings())).close()
    mark_project(Settings(data_dir=tmp_path / ".mnogobase"))
    monkeypatch.setattr(cli.common, "preflight", lambda *a: None)
    monkeypatch.setattr(cli.common, "build_app", lambda *a: SimpleNamespace(close=lambda: None))
    scored = []

    class Retriever:
        def __init__(self, broken=False):
            self.broken = broken

        def retrieve(self, query, k, alt_queries=()):
            if self.broken:
                raise RuntimeError("graph is down")
            return [ContextItem(kind="chunk", ref="c", text="Answer")]

    class Judge:
        def __init__(self, *a):
            pass

        def metrics(self, names):
            class Metric:
                async def ascore(self, **kwargs):
                    scored.append(1)
                    return SimpleNamespace(value=1.0)

            return {name: Metric() for name in names}

        async def close(self):
            pass

    monkeypatch.setattr(core, "RagasJudge", Judge)
    llm = FakeLLM(lambda *a: "Answer [1]")
    retrievers = {"rag": Retriever(), "wiki": Retriever(), "graph": Retriever(broken=True)}
    monkeypatch.setattr(
        cli.common, "answer_setup", lambda app: (retrievers, Answerer(llm, 1000), None)
    )
    result = runner.invoke(cli.app, ["eval", "benchmark", str(offline), "--output", "run"])
    assert result.exit_code == 1, result.output  # прогон неполный
    assert scored and (tmp_path / "run/details.csv").is_file()  # но оценка выполнена


def test_latest_records_reports_a_record_without_its_key(tmp_path):
    path = tmp_path / "rows.jsonl"
    path.write_text('{"id": "a"}\n{"value": 1}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="record 2: no id"):
        core.latest_records(path)
