import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console
from typer.testing import CliRunner

from mnogobase import cli
from mnogobase.doctor import Check
from mnogobase.models import Answer, Source
from mnogobase.registry import Registry

runner = CliRunner()


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Unit tests never touch the network or reconfigure the process-wide logging."""
    monkeypatch.setattr(cli, "configure_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "run_checks", lambda settings, **kwargs: [Check("qdrant", True, "ok")])


def make_project(root: Path) -> None:
    Registry(root / ".mnogobase" / "state.db").close()


def test_status_on_empty_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    make_project(tmp_path)
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0, result.output
    assert "files: 0" in result.output


def test_status_outside_a_project_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0, result.output
    assert "No mnogobase project here" in result.output and "state.db" in result.output
    assert not (tmp_path / ".mnogobase").exists()


@pytest.mark.parametrize("args", [["ask", "what is attention?"], ["compare", "what?"]])
def test_ask_and_compare_refuse_outside_a_project(tmp_path, monkeypatch, args):
    monkeypatch.chdir(tmp_path)  # no .mnogobase/state.db here
    monkeypatch.setattr(cli, "build_app", lambda settings: pytest.fail("build_app ran"))
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 2, result.output
    assert "No mnogobase project here" in result.output and "state.db" in result.output
    assert not (tmp_path / ".mnogobase").exists()


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
    make_project(tmp_path)
    called = []
    monkeypatch.setattr(cli, "build_app", lambda settings: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(cli, "do_reset", lambda application: called.append(application))
    declined = runner.invoke(cli.app, ["reset"], input="n\n")
    assert declined.exit_code == 1 and called == []
    accepted = runner.invoke(cli.app, ["reset", "--yes"])
    assert accepted.exit_code == 0 and len(called) == 1


def test_reset_prompt_lists_the_resolved_targets(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    make_project(tmp_path)
    (tmp_path / "config.yaml").write_text(
        "neo4j:\n  uri: bolt://graph.example:7999\n"
        "qdrant:\n  url: http://vectors.example:6399\n  prefix: proj_\n"
        "wiki:\n  dir: kb\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "do_reset", lambda application: pytest.fail("declined reset ran"))
    result = runner.invoke(cli.app, ["reset"], input="n\n")
    assert result.exit_code == 1, result.output
    assert "bolt://graph.example:7999" in result.output
    assert "http://vectors.example:6399" in result.output and "'proj_'" in result.output
    assert str((tmp_path / ".mnogobase").resolve()) in result.output
    assert str((tmp_path / "kb").resolve()) in result.output


@pytest.mark.parametrize("args", [["reset"], ["reset", "--yes"]])
def test_reset_refuses_outside_a_project(tmp_path, monkeypatch, args):
    monkeypatch.chdir(tmp_path)  # no .mnogobase/state.db here
    monkeypatch.setattr(cli, "build_app", lambda settings: pytest.fail("build_app ran"))
    monkeypatch.setattr(cli, "do_reset", lambda application: pytest.fail("reset ran"))
    result = runner.invoke(cli.app, args, input="y\n")
    assert result.exit_code == 2, result.output
    assert "No mnogobase project" in result.output and "state.db" in result.output
    assert not (tmp_path / ".mnogobase").exists()


@pytest.mark.parametrize(
    ("args", "expected"),
    [(["reset", "--yes"], {"qdrant", "neo4j"}), (["reindex"], {"ollama", "qdrant", "neo4j"})],
)
def test_reset_and_reindex_preflight(tmp_path, monkeypatch, args, expected):
    monkeypatch.chdir(tmp_path)
    make_project(tmp_path)
    asked: list[tuple[str, ...]] = []

    def failing(settings, only=None):
        asked.append(tuple(only or ()))
        return [Check("neo4j", False, "ServiceUnavailable: down")]

    monkeypatch.setattr(cli, "run_checks", failing)
    monkeypatch.setattr(cli, "build_app", lambda settings: pytest.fail("build_app ran"))
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 2, result.output
    assert "ServiceUnavailable: down" in result.output
    assert set(asked[0]) == expected


@pytest.mark.parametrize(
    "content",
    ["wiki: [unclosed\n", "wiki:\n  min_mentions: lots\n", "- just\n- a list\n"],
)
def test_invalid_config_is_a_clear_error(tmp_path, monkeypatch, content):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(content, encoding="utf-8")
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 2, result.output
    assert "Invalid configuration" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


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
    make_project(tmp_path)
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


def test_wiki_build_preflight_failure_stops_with_exit_2(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    asked: list[tuple[str, ...]] = []

    def failing(settings, only=None):
        asked.append(tuple(only or ()))
        return [Check("neo4j", False, "ServiceUnavailable: down")]

    monkeypatch.setattr(cli, "run_checks", failing)
    monkeypatch.setattr(cli, "build_app", lambda settings: pytest.fail("build_app ran"))
    result = runner.invoke(cli.app, ["wiki", "build"])
    assert result.exit_code == 2, result.output
    assert "ServiceUnavailable: down" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert set(asked[0]) == {"ollama", "llm", "qdrant", "neo4j"}


def test_wiki_build_finishes_pending_removals_first(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls: list[str] = []

    async def build(**kwargs):
        calls.append(f"build deleted={kwargs['deleted']}")
        return SimpleNamespace(
            created=[], updated=[], deleted=kwargs["deleted"], skipped=[], failed=[]
        )

    def finish():
        calls.append("finish")
        return ["Softmax"]

    stub = SimpleNamespace(
        pipeline=SimpleNamespace(prepare=lambda resume: None, finish_pending_removals=finish),
        wiki=SimpleNamespace(build=build),
        close=lambda: None,
    )
    monkeypatch.setattr(cli, "build_app", lambda settings: stub)
    result = runner.invoke(cli.app, ["wiki", "build"])
    assert result.exit_code == 0, result.output
    assert calls == ["finish", "build deleted=['Softmax']"]
    assert "deleted 1" in result.output


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


@pytest.mark.parametrize("args", [["status"], ["ask", "q?"], ["compare", "q?"]])
def test_read_only_commands_outside_a_project_do_not_start_file_logging(
    tmp_path, monkeypatch, args
):
    monkeypatch.chdir(tmp_path)
    started: list[Path] = []
    monkeypatch.setattr(cli, "configure_logging", lambda logs_dir, **kw: started.append(logs_dir))
    runner.invoke(cli.app, args)
    assert started == []  # configure_logging would create logs/ in this directory
