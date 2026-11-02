from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
import structlog

from mnogobase.log import configure_logging, get_logger


def _rows(directory):
    return [
        json.loads(line)
        for line in (directory / "mnogobase.jsonl").read_text(encoding="utf-8").splitlines()
    ]


def test_simultaneous_team_scopes_route_json_logs_and_restore_context(tmp_path):
    from mnogobase.log import team_logging

    base, a, b = (tmp_path / name for name in ("base", "A", "B"))
    configure_logging(base, console=False)
    logger = get_logger("teams")
    logger.info("legacy_before")
    barrier = threading.Barrier(2)
    handler_ids = tuple(id(handler) for handler in logging.getLogger().handlers)

    def write(team_id, directory):
        before = structlog.contextvars.get_contextvars()
        with team_logging(team_id, directory):
            barrier.wait(timeout=3)
            for index in range(20):
                logger.info("team_event", index=index)
            logging.getLogger("team_external").warning("foreign_event")
            barrier.wait(timeout=3)
        assert structlog.contextvars.get_contextvars() == before

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(write, team_id, directory) for team_id, directory in (("A", a), ("B", b))
        ]
        for future in futures:
            future.result(timeout=5)
    logger.info("legacy_after")
    assert tuple(id(handler) for handler in logging.getLogger().handlers) == handler_ids
    for team_id, directory in (("A", a), ("B", b)):
        records = _rows(directory)
        assert len(records) == 21
        assert {record["team_id"] for record in records} == {team_id}
        assert all(record["event"] in {"team_event", "foreign_event"} for record in records)
        assert all("ts" in record and "level" in record for record in records)
        assert str(tmp_path) not in (directory / "mnogobase.jsonl").read_text(encoding="utf-8")
    assert [record["event"] for record in _rows(base)] == ["legacy_before", "legacy_after"]
    assert all("team_id" not in record for record in _rows(base))


def test_nested_scopes_restore_prior_destination_and_structlog_context_on_exception(tmp_path):
    from mnogobase.log import team_logging

    base, outer, inner = (tmp_path / name for name in ("base", "outer", "inner"))
    configure_logging(base, console=False)
    before = structlog.contextvars.get_contextvars()
    structlog.contextvars.bind_contextvars(team_id="previous", run_id="existing")
    logger = get_logger("nested")
    try:
        with team_logging("outer", outer):
            logger.info("outer_before")
            with pytest.raises(ValueError), team_logging("inner", inner):
                structlog.contextvars.bind_contextvars(run_id="inner_run", transient="private")
                logger.info("inner_event")
                raise ValueError("stop")
            assert structlog.contextvars.get_contextvars() == {
                "team_id": "outer",
                "run_id": "existing",
            }
            logger.info("outer_after")
        assert structlog.contextvars.get_contextvars() == {
            "team_id": "previous",
            "run_id": "existing",
        }
        logger.info("legacy_after")
    finally:
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(**before)
    assert [record["event"] for record in _rows(outer)] == ["outer_before", "outer_after"]
    assert _rows(inner)[0]["team_id"] == "inner"
    assert _rows(base)[0]["team_id"] == "previous"


def test_team_routing_cache_closes_evicted_handlers_and_reconfiguration(tmp_path, monkeypatch):
    import mnogobase.log as log_module
    from mnogobase.log import team_logging

    created = []
    original = log_module.RotatingFileHandler

    class RecordingHandler(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(log_module, "RotatingFileHandler", RecordingHandler)
    configure_logging(tmp_path / "base", console=False)
    for index in range(80):
        with team_logging(str(index), tmp_path / f"team-{index}"):
            get_logger("cache").info("entry")
    assert len(created) == 80
    assert sum(handler.stream is not None for handler in created) <= 64
    assert any(handler.stream is None for handler in created)
    assert all(handler.maxBytes == 10_000_000 and handler.backupCount == 5 for handler in created)
    configure_logging(tmp_path / "new-base", console=False)
    assert all(handler.stream is None for handler in created)
