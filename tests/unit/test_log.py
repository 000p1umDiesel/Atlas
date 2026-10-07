import json
import logging

import pytest
import structlog

from mnogobase.log import (
    bind_context,
    configure_logging,
    get_logger,
    log_stage,
    new_run_id,
    unbind_context,
)


def _read(logs_dir):
    for h in logging.getLogger().handlers:
        h.flush()
    lines = (logs_dir / "mnogobase.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def test_jsonl_contains_context_fields(tmp_path):
    configure_logging(tmp_path, console=False)
    run_id = new_run_id()
    get_logger("t").info("hello", doc_id="d1", text="привет")
    rec = _read(tmp_path)[-1]
    assert rec["event"] == "hello"
    assert rec["run_id"] == run_id
    assert rec["doc_id"] == "d1"
    assert rec["text"] == "привет"
    assert rec["level"] == "info"
    assert "ts" in rec
    structlog.contextvars.clear_contextvars()


def test_log_stage_success_and_failure(tmp_path):
    configure_logging(tmp_path, console=False)
    log = get_logger("t")
    with log_stage(log, "parse", doc_id="d1"):
        pass
    with pytest.raises(ValueError), log_stage(log, "chunk", doc_id="d1"):
        raise ValueError("boom")
    done, failed = _read(tmp_path)[-2:]
    assert done["event"] == "stage_done" and done["stage"] == "parse"
    assert isinstance(done["duration_ms"], int)
    assert failed["event"] == "stage_failed" and failed["stage"] == "chunk"
    assert "boom" in failed["error"]


def test_bind_and_unbind_context(tmp_path):
    configure_logging(tmp_path, console=False)
    log = get_logger("t")
    bind_context(doc_path="a.pdf")
    log.info("bound")
    unbind_context("doc_path")
    log.info("unbound")
    bound, unbound = _read(tmp_path)[-2:]
    assert bound["event"] == "bound" and bound["doc_path"] == "a.pdf"
    assert unbound["event"] == "unbound" and "doc_path" not in unbound
