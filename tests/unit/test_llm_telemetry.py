import asyncio

import httpx
import pytest
from tenacity import wait_none

from mnogobase.config import LLMSettings, ServiceSettings
from mnogobase.llm.client import OpenAICompatLLM
from mnogobase.llm.telemetry import usage_scope
from tests.unit.test_llm_client import _completion


async def test_records_each_attempt_with_unknown_error_cost_and_safe_context():
    events = []
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(500, json={"error": {"message": "secret document"}})
        return httpx.Response(200, json=_completion("ok"))

    llm = OpenAICompatLLM(
        LLMSettings(model="m"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        retry_wait=wait_none(),
    )
    try:
        with usage_scope("a" * 32, events.append, job_id=42):
            assert (
                await llm.complete([{"role": "user", "content": "private"}], task="answer") == "ok"
            )
        assert len(events) == 4  # durable start then result for each actual attempt
        assert events[0].id == events[1].id != events[2].id == events[3].id
        assert events[1].status == "error" and events[1].tokens_in is None
        assert events[3].tokens_in == 3 and events[3].tokens_out == 5
        assert all(e.team_id == "a" * 32 and e.job_id == 42 for e in events)
        assert "private" not in repr(events) and "secret document" not in repr(events)
    finally:
        await llm.aclose()


async def test_concurrent_scopes_and_missing_usage():
    events = []
    response = _completion("ok")
    response.pop("usage")
    llm = OpenAICompatLLM(
        LLMSettings(model="m"),
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=response))
        ),
    )

    async def call(team):
        with usage_scope(team, events.append):
            await llm.complete([], task="query")

    try:
        await asyncio.gather(call("a" * 32), call("b" * 32))
        assert {e.team_id for e in events} == {"a" * 32, "b" * 32}
        assert all(e.tokens_in is None for e in events)
    finally:
        await llm.aclose()


def test_rates_and_thresholds_validate():
    config = ServiceSettings(
        llm_rates={
            "m": {"input_per_million": "1.25", "output_per_million": "2.5", "currency": "USD"}
        }
    )
    assert str(config.llm_rates["m"].input_per_million) == "1.25"
    for value in ("-1", "NaN", "Infinity"):
        with pytest.raises(ValueError):
            ServiceSettings(
                llm_rates={
                    "m": {"input_per_million": value, "output_per_million": 1, "currency": "USD"}
                }
            )
    with pytest.raises(ValueError):
        ServiceSettings(monitor={"worker_stale_s": 20, "heartbeat_s": 30})


async def test_bound_recorder_covers_operator_cli_without_request_scope():
    events = []
    llm = OpenAICompatLLM(
        LLMSettings(model="m"),
        team_id="a" * 32,
        usage_recorder=events.append,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_completion("ok")))
        ),
    )
    try:
        await llm.complete([], task="wiki")
        assert events[-1].team_id == "a" * 32 and events[-1].tokens_in == 3
        with usage_scope("b" * 32, events.append, job_id=7):
            await llm.complete([], task="answer")
        assert events[-1].team_id == "b" * 32 and events[-1].job_id == 7
    finally:
        await llm.aclose()
