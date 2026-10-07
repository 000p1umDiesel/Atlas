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


def test_tracebacks_do_not_include_frame_locals(tmp_path):
    configure_logging(tmp_path, console=False)

    def leaky():
        api_key = "sk-very-secret-value"  # noqa: F841 — a local that must not be logged
        raise RuntimeError("boom")

    try:
        leaky()
    except RuntimeError:
        get_logger("t").error("failed", exc_info=True)
    raw = (tmp_path / "mnogobase.jsonl").read_text(encoding="utf-8")
    rec = _read(tmp_path)[-1]
    assert rec["event"] == "failed"
    assert rec["exception"]  # the traceback itself is still structured in the record
    assert "sk-very-secret-value" not in raw
    assert "api_key" not in raw


def test_third_party_noise_is_quieted_but_warnings_pass(tmp_path):
    from huggingface_hub.utils import are_progress_bars_disabled
    from transformers.utils import logging as hf_logging

    configure_logging(tmp_path, console=False)
    ocr = logging.getLogger("RapidOCR")
    ocr.setLevel(logging.INFO)  # what rapidocr does when docling imports it later

    def passes(level: int) -> bool:
        record = logging.LogRecord("RapidOCR", level, __file__, 1, "msg", None, None)
        return bool(ocr.filter(record))

    assert not passes(logging.INFO)
    assert passes(logging.WARNING) and passes(logging.ERROR)
    assert are_progress_bars_disabled()  # Hugging Face downloads
    assert not hf_logging.is_progress_bar_enabled()  # transformers "Loading weights"
