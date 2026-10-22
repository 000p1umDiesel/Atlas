import json

from typer.testing import CliRunner

from mnogobase import cli
from mnogobase.log_view import LogFilter, follow, last_run_id, render, tail

LLM = {
    "event": "llm_call",
    "task": "extract",
    "model": "gpt",
    "tokens_in": 1062,
    "tokens_out": 737,
    "duration_ms": 7039,
    "level": "info",
    "ts": "2026-10-07T13:35:24Z",
    "run_id": "2659c0ccb5cf",
    "doc_id": "bdfaa68d8984f0dc",
}
HTTP = {"event": 'HTTP Request: POST http://x "HTTP/1.1 200 OK"', "level": "info", "ts": "t"}
WARN = {"event": "extract_partial", "failed": 2, "total": 44, "level": "warning", "ts": "t"}


def write(path, *records, partial: str = ""):
    with path.open("a", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record) + "\n")
        file.write(partial)


def test_tail_filters_by_level_run_event_and_hides_http(tmp_path):
    log = tmp_path / "mnogobase.jsonl"
    write(log, LLM, HTTP, WARN, {**LLM, "run_id": "ffff00"}, partial="{broken")
    assert [r["event"] for r in tail(log, 10, LogFilter())] == [
        "llm_call",
        "extract_partial",
        "llm_call",
    ]
    assert tail(log, 1, LogFilter()) == [{**LLM, "run_id": "ffff00"}]
    assert [r["event"] for r in tail(log, 10, LogFilter(level="warning"))] == ["extract_partial"]
    assert len(tail(log, 10, LogFilter(run_id="2659c0"))) == 1
    assert len(tail(log, 10, LogFilter(http=True))) == 4
    assert tail(log, 10, LogFilter(event="partial")) == [WARN]
    assert last_run_id(log) == "ffff00"


def test_render_summarises_known_events_and_shows_errors():
    text = render(LLM).plain
    assert "llm_call" in text and "extract · gpt · 1062→737 tok · 7.0 s" in text
    assert text.endswith("run 2659c0 doc bdfaa6")
    assert "failed=2 total=44" in render(WARN).plain
    failed = {
        "event": "stage_failed",
        "stage": "parse",
        "path": "/docs/a.pdf",
        "error_type": "VisionError",
        "error": "no description",
        "level": "error",
        "ts": "2026-10-07T13:35:24Z",
        "exception": [{"exc_type": "VisionError", "exc_value": "x", "frames": [
            {"filename": "docling_parser.py", "lineno": 7}
        ]}],
    }  # fmt: skip
    lines = render(failed).plain.splitlines()
    assert (
        "ERROR stage_failed" in lines[0]
        and "parse · a.pdf · VisionError: no description" in lines[0]
    )
    assert lines[1] == "    VisionError: x at docling_parser.py:7"


def test_follow_reads_new_complete_lines_and_survives_rotation(tmp_path):
    log = tmp_path / "mnogobase.jsonl"
    write(log, LLM)  # история: её показывает tail, follow начинает с конца
    steps = iter(
        [
            lambda: write(log, WARN, partial='{"event": "half'),
            lambda: write(log, partial='", "level": "info", "ts": "t"}\n'),
            lambda: (log.rename(tmp_path / "mnogobase.jsonl.1"), write(log, {**WARN, "total": 9})),
        ]
    )
    done: list[bool] = []

    def sleep(seconds):
        step = next(steps, None)
        if step is None:
            done.append(True)
        else:
            step()

    events = list(follow(log, LogFilter(), sleep=sleep, stop=lambda: bool(done)))
    assert [e["event"] for e in events] == ["extract_partial", "half", "extract_partial"]
    assert events[-1]["total"] == 9


def test_logs_command_prints_recent_events(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MNOGOBASE_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(cli, "configure_logging", lambda *a, **k: None)
    (tmp_path / "logs").mkdir()
    write(tmp_path / "logs" / "mnogobase.jsonl", LLM, WARN)
    result = CliRunner().invoke(cli.app, ["logs", "--level", "warning"])
    assert result.exit_code == 0, result.output
    assert "extract_partial" in result.output and "llm_call" not in result.output
    bad = CliRunner().invoke(cli.app, ["logs", "--level", "loud"])
    assert bad.exit_code == 2
