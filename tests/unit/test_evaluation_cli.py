import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from mnogobase import cli
from mnogobase.config import Settings
from mnogobase.evaluation import core
from mnogobase.models import ContextItem
from mnogobase.registry import Registry
from mnogobase.retrieval.answer import Answerer
from tests.fakes import FakeLLM

runner = CliRunner()


@pytest.fixture
def offline(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "configure_logging", lambda *a: None)
    path = tmp_path / "questions.jsonl"
    path.write_text(json.dumps({"id": "q", "question": "Q?", "reference": "A"}) + "\n")
    return path


def test_cli_run_uses_three_modes_and_closes_app(offline, tmp_path, monkeypatch):
    Registry(tmp_path / ".mnogobase/state.db").close()
    closed = []
    monkeypatch.setattr(cli, "_preflight", lambda *a: None)
    monkeypatch.setattr(
        cli, "build_app", lambda *a: SimpleNamespace(close=lambda: closed.append(True))
    )

    class Retriever:
        def retrieve(self, query, k, alt_queries=()):
            return [ContextItem(kind="chunk", ref="c", text="A")]

    monkeypatch.setattr(
        cli,
        "_answer_setup",
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
    monkeypatch.setattr(cli, "build_app", lambda *a: pytest.fail("app was built"))
    monkeypatch.setattr(cli, "_preflight", lambda *a: pytest.fail("preflight ran"))
    result = runner.invoke(cli.app, ["eval", "report", str(run)])
    assert result.exit_code == 0, result.output
    assert (run / "summary.csv").is_file()
    # Missing inference records are skipped without constructing judge clients.
    result = runner.invoke(cli.app, ["eval", "score", str(run)])
    assert result.exit_code == 1, result.output
    assert "inference_failed_or_missing" in (run / "details.csv").read_text()


def test_cli_invalid_dataset_fails_before_services(offline, tmp_path, monkeypatch):
    offline.write_text('{"id":"x","question":""}\n')
    monkeypatch.setattr(cli, "build_app", lambda *a: pytest.fail("app was built"))
    result = runner.invoke(cli.app, ["eval", "run", str(offline), "--output", "run"])
    assert result.exit_code == 2, result.output
    assert "id and question" in result.output


def test_cli_unknown_metric_rejected_without_judge(offline, tmp_path):
    core.prepare_run(offline, tmp_path / "run", Settings())
    result = runner.invoke(cli.app, ["eval", "score", "run", "--metric", "invented"])
    assert result.exit_code == 2, result.output
    assert "metrics" in result.output


def test_dataset_copy_is_standalone():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    assert (root / "eval/questions.jsonl").read_bytes() == (
        root / "hybrid_rag_notebooks/data/questions.jsonl"
    ).read_bytes()
