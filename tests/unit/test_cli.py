import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console
from typer.testing import CliRunner

from mnogobase import cli
from mnogobase.doctor import Check
from mnogobase.extraction.extractor import PROMPT_VERSION
from mnogobase.maintenance import ReindexError
from mnogobase.models import Answer, Source
from mnogobase.pipeline import PendingRemovalError
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

    def failing(settings, only=None, **kwargs):
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

    def failing(settings, only=None, **kwargs):
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

    def failing(settings, only=None, **kwargs):
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
        return SimpleNamespace(
            processed=["x.md"], skipped=[], failed={}, wiki=wiki, types_warning=None
        )

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


STUCK = PendingRemovalError("cannot finish removing old document version(s) abcd1234abcd1234 (x)")


def test_ingest_with_a_stuck_pending_removal_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def ingest(targets, **kwargs):
        raise STUCK

    stub = SimpleNamespace(pipeline=SimpleNamespace(ingest=ingest), close=lambda: None)
    monkeypatch.setattr(cli, "build_app", lambda settings: stub)
    result = runner.invoke(cli.app, ["ingest", "docs"])
    assert result.exit_code == 2, result.output
    assert "abcd1234abcd1234" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_wiki_build_with_a_stuck_pending_removal_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def finish():
        raise STUCK

    stub = SimpleNamespace(
        pipeline=SimpleNamespace(prepare=lambda resume: None, finish_pending_removals=finish),
        close=lambda: None,
    )
    monkeypatch.setattr(cli, "build_app", lambda settings: stub)
    result = runner.invoke(cli.app, ["wiki", "build"])
    assert result.exit_code == 2, result.output
    assert "abcd1234abcd1234" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_status_shows_pending_removals(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    registry = Registry(tmp_path / ".mnogobase" / "state.db")
    registry.put_removal("abcd1234abcd1234", "{}")
    registry.close()
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0, result.output
    assert "pending document removals: 1" in result.output
    assert "abcd1234abcd1234" in result.output


def test_doctor_warning_is_shown_but_does_not_fail(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    warning = Check("types", True, "entity types changed: run `mnogobase reset`", warn=True)
    monkeypatch.setattr(cli, "run_checks", lambda settings: [Check("device", True, "mps"), warning])
    result = runner.invoke(cli.app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "warn" in result.output and "mnogobase reset" in result.output


def _extracted_project(root: Path, types_signature: str) -> None:
    registry = Registry(root / ".mnogobase" / "state.db")
    registry.put_extraction("c1", f"{PROMPT_VERSION}:{types_signature}", "m", "{}")
    registry.close()


def test_status_warns_when_the_entity_types_changed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _extracted_project(tmp_path, "0123456789ab")
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0, result.output
    assert "entity types changed" in result.output
    assert "mnogobase reset" in result.output and "mnogobase ingest" in result.output


def test_status_is_quiet_when_the_entity_types_match(tmp_path, monkeypatch):
    from mnogobase.config import DEFAULT_ENTITY_TYPES
    from mnogobase.extraction.extractor import entity_types_signature

    monkeypatch.chdir(tmp_path)
    _extracted_project(tmp_path, entity_types_signature(DEFAULT_ENTITY_TYPES))
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0, result.output
    assert "entity types changed" not in result.output


def test_ingest_prints_the_entity_types_warning(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def ingest(targets, **kwargs):
        return SimpleNamespace(
            processed=[], skipped=["x.md"], failed={}, wiki=None, types_warning="TYPES CHANGED"
        )

    stub = SimpleNamespace(pipeline=SimpleNamespace(ingest=ingest), close=lambda: None)
    monkeypatch.setattr(cli, "build_app", lambda settings: stub)
    result = runner.invoke(cli.app, ["ingest", "docs"])
    assert result.exit_code == 0, result.output
    assert "TYPES CHANGED" in result.output


@pytest.mark.parametrize(
    ("args", "check_dim"),
    [(["reindex"], False), (["ingest", "docs"], True), (["wiki", "build"], True)],
)
def test_only_reindex_accepts_a_collection_of_another_dimension(
    tmp_path, monkeypatch, args, check_dim
):
    monkeypatch.chdir(tmp_path)
    make_project(tmp_path)
    seen: list[bool] = []

    def checks(settings, only=None, check_dim=True):
        seen.append(check_dim)
        return [Check("qdrant", False, "stop here")]

    monkeypatch.setattr(cli, "run_checks", checks)
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 2, result.output
    assert seen == [check_dim]


def test_reindex_with_a_wrong_embedder_dimension_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    make_project(tmp_path)

    def failing(application, **kwargs):
        raise ReindexError("embedder check failed: model returned 768 dims; nothing was changed")

    monkeypatch.setattr(cli, "build_app", lambda settings: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(cli, "do_reindex", failing)
    result = runner.invoke(cli.app, ["reindex"])
    assert result.exit_code == 2, result.output
    assert "model returned 768 dims" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def _drive(progress) -> None:
    """What a one-file ingest with a wiki update reports."""
    progress.files_found(1)
    progress.file_started("/docs/statya.pdf")
    for stage in ("parse", "chunk", "embed"):
        progress.step(stage)
    progress.step("extract", 3)
    progress.advance()
    progress.advance()
    progress.advance(failed=True)
    progress.step("graph", 2)
    progress.advance(2)
    progress.file_done("processed")
    progress.step("wiki draft", 1)
    progress.advance()
    progress.step("wiki write", 1)
    progress.advance()


def test_ingest_reports_progress_and_prints_only_the_summary_off_a_terminal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    seen = []

    async def ingest(targets, **kwargs):
        seen.append(kwargs["progress"])
        _drive(kwargs["progress"])
        return SimpleNamespace(
            processed=["/docs/statya.pdf"], skipped=[], failed={}, wiki=None, types_warning=None
        )

    stub = SimpleNamespace(pipeline=SimpleNamespace(ingest=ingest), close=lambda: None)
    monkeypatch.setattr(cli, "build_app", lambda settings: stub)
    result = runner.invoke(cli.app, ["ingest", "docs"])
    assert result.exit_code == 0, result.output
    assert len(seen) == 1
    # CliRunner is not a terminal: no progress frames, just the summary as before
    lines = result.output.splitlines()
    assert len(lines) == 1 and lines[0].startswith("run ")
    assert lines[0].endswith("processed 1, skipped 0, failed 0")


def test_progress_shows_files_the_current_file_its_stage_and_failures(monkeypatch):
    monkeypatch.setattr(cli, "console", Console(file=io.StringIO(), force_terminal=True))
    screen = Console(record=True, width=120, file=io.StringIO())
    with cli._progress() as progress:
        progress.files_found(12)
        progress.file_started("/docs/a/statya.pdf")
        progress.step("extract")
        progress.step("extract", 96)
        for _ in range(41):
            progress.advance()
        progress.advance(failed=True)
        screen.print(progress.bars)
        progress.file_done("processed")
        progress.step("wiki draft", 5)
        progress.advance()
        screen.print(progress.bars)
    first, second = screen.export_text().split("files", 2)[1:]
    assert "0/12" in first
    assert "statya.pdf · extract" in first and "42/96" in first and "1 failed" in first
    assert "1/12" in second and "1 processed" in second
    assert "wiki draft" in second and "1/5" in second and "statya.pdf" not in second


def test_wiki_build_and_reindex_report_progress(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    make_project(tmp_path)
    seen: list[str] = []

    async def build(**kwargs):
        seen.append(f"wiki {type(kwargs['progress']).__name__}")
        return SimpleNamespace(created=[], updated=[], deleted=[], skipped=[], failed=[])

    def reindex(application, **kwargs):
        seen.append(f"reindex {type(kwargs['progress']).__name__}")
        return {"chunks": 1, "entities": 2, "wiki_sections": 3}

    stub = SimpleNamespace(
        pipeline=SimpleNamespace(prepare=lambda resume: None, finish_pending_removals=list),
        wiki=SimpleNamespace(build=build),
        close=lambda: None,
    )
    monkeypatch.setattr(cli, "build_app", lambda settings: stub)
    monkeypatch.setattr(cli, "do_reindex", reindex)
    assert runner.invoke(cli.app, ["wiki", "build"]).exit_code == 0
    result = runner.invoke(cli.app, ["reindex"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "reindexed: 1 chunks, 2 entities, 3 wiki sections"
    assert seen == ["wiki RichProgress", "reindex RichProgress"]
