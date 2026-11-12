import asyncio

import httpx
import pytest
from tenacity import wait_none

from mnogobase.core.config import LLMSettings, ServiceSettings
from mnogobase.llm.client import OpenAICompatLLM
from mnogobase.llm.telemetry import job_scope
from tests.unit.test_llm_client import _completion


async def test_records_each_attempt_with_job_and_without_prompt_text():
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
        usage_recorder=events.append,
    )
    try:
        with job_scope(42):
            assert (
                await llm.complete([{"role": "user", "content": "private"}], task="answer") == "ok"
            )
        assert len(events) == 4  # durable start then result for each actual attempt
        assert events[0].id == events[1].id != events[2].id == events[3].id
        assert events[1].status == "error" and events[1].tokens_in is None
        assert events[3].tokens_in == 3 and events[3].tokens_out == 5
        assert all(e.job_id == 42 for e in events)
        assert "private" not in repr(events) and "secret document" not in repr(events)
    finally:
        await llm.aclose()


async def test_concurrent_job_scopes_and_missing_usage():
    events = []
    response = _completion("ok")
    response.pop("usage")
    llm = OpenAICompatLLM(
        LLMSettings(model="m"),
        usage_recorder=events.append,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=response))
        ),
    )

    async def call(job_id):
        with job_scope(job_id):
            await llm.complete([], task="query")

    try:
        await asyncio.gather(call(1), call(2), llm.complete([], task="query"))
        assert sorted(e.job_id or 0 for e in events) == [0, 0, 1, 1, 2, 2]
        assert all(e.tokens_in is None for e in events)
    finally:
        await llm.aclose()


def test_rates_validate():
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


async def test_without_recorder_nothing_is_recorded():
    llm = OpenAICompatLLM(
        LLMSettings(model="m"),
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_completion("ok")))
        ),
    )
    try:
        assert await llm.complete([], task="wiki") == "ok"
    finally:
        await llm.aclose()
