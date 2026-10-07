import io
from types import SimpleNamespace

import pytest
from rich.console import Console
from typer.testing import CliRunner

from mnogobase import cli
from mnogobase.doctor import Check
from mnogobase.models import Answer, Source

runner = CliRunner()


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Unit tests never touch the network or reconfigure the process-wide logging."""
    monkeypatch.setattr(cli, "configure_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "run_checks", lambda settings, **kwargs: [Check("qdrant", True, "ok")])


def test_status_on_empty_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0, result.output
    assert "files: 0" in result.output


def test_doctor_exit_code_reflects_failures(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli,
        "run_checks",
        lambda settings: [
            Check("device", True, "mps"),
            Check("neo4j", False, "ServiceUnavailable"),
        ],
    )
    result = runner.invoke(cli.app, ["doctor"])
    assert result.exit_code == 1
    assert "neo4j" in result.output and "fail" in result.output


def test_reset_requires_confirmation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    called = []
    monkeypatch.setattr(cli, "build_app", lambda settings: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(cli, "do_reset", lambda application: called.append(application))
    declined = runner.invoke(cli.app, ["reset"], input="n\n")
    assert declined.exit_code == 1 and called == []
    accepted = runner.invoke(cli.app, ["reset", "--yes"])
    assert accepted.exit_code == 0 and len(called) == 1


def test_ingest_without_paths_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    stub = SimpleNamespace(registry=SimpleNamespace(failed_paths=lambda: []), close=lambda: None)
    monkeypatch.setattr(cli, "build_app", lambda settings: stub)
    result = runner.invoke(cli.app, ["ingest", "--retry-failed"])
    assert result.exit_code == 0
    assert "Nothing to ingest" in result.output


def test_missing_config_file_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, ["--config", "nope.yaml", "status"])
    assert result.exit_code == 2
    assert "Config file not found" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


@pytest.mark.parametrize(
    "args", [["ingest", "docs"], ["ask", "what is attention?"], ["compare", "what?"]]
)
def test_preflight_failure_stops_with_exit_2(tmp_path, monkeypatch, args):
    monkeypatch.chdir(tmp_path)
    asked: list[tuple[str, ...]] = []

    def failing(settings, only=None):
        asked.append(tuple(only or ()))
        return [Check("qdrant", True, "ok"), Check("ollama", False, "ConnectError: refused")]

    def no_app(settings):
        raise AssertionError("build_app must not run after a failed preflight")

    monkeypatch.setattr(cli, "run_checks", failing)
    monkeypatch.setattr(cli, "build_app", no_app)
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 2, result.output
    assert "ollama" in result.output and "ConnectError: refused" in result.output
    assert set(asked[0]) == {"ollama", "llm", "qdrant", "neo4j"}


def test_ingest_summary_reports_wiki_failures(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def ingest(targets, **kwargs):
        wiki = SimpleNamespace(created=["A"], updated=[], deleted=[], skipped=[], failed=["Bad"])
        return SimpleNamespace(processed=["x.md"], skipped=[], failed={}, wiki=wiki)

    stub = SimpleNamespace(pipeline=SimpleNamespace(ingest=ingest), close=lambda: None)
    monkeypatch.setattr(cli, "build_app", lambda settings: stub)
    result = runner.invoke(cli.app, ["ingest", "docs"])
    assert result.exit_code == 1, result.output
    assert "processed 1" in result.output
    assert "failed 1" in result.output and "Bad" in result.output


def test_print_answer_shows_source_ref(monkeypatch):
    printed = []
    monkeypatch.setattr(cli.console, "print", lambda *args, **kwargs: printed.extend(args))
    answer = Answer(
        question="q",
        mode="rag",
        text="Attention [1].",
        sources=[Source(n=1, kind="chunk", ref="abc:00001", path="/d/a.md", page=2, cited=True)],
    )
    cli._print_answer(answer)
    table = printed[-1]
    headers = [col.header for col in table.columns]
    assert "ref" in headers
    assert list(table.columns[headers.index("ref")].cells) == ["abc:00001"]


def test_print_answer_shows_document_text_verbatim(monkeypatch):
    recorder = Console(record=True, width=300, file=io.StringIO())
    monkeypatch.setattr(cli, "console", recorder)
    text = "see [/foo] and [link here](http://x) [1]"
    answer = Answer(
        question="q",
        mode="rag",
        text=text,
        sources=[Source(n=1, kind="chunk", ref="abc:00001", snippet="[b]raw[/b]", cited=True)],
    )
    cli._print_answer(answer)  # Rich would raise MarkupError on unescaped "[/foo]"
    output = recorder.export_text()
    assert text in output and "[b]raw[/b]" in output
