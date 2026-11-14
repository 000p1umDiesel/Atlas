import json

import httpx
import openai
import pytest
from pydantic import BaseModel
from tenacity import wait_none

from mnogobase.core.config import LLMSettings
from mnogobase.llm.client import (
    OpenAICompatLLM,
    RateLimiter,
    StructuredOutputError,
    clean_json,
    strict_json_schema,
)


class Item(BaseModel):
    title: str
    tags: list[str] = []


class Box(BaseModel):
    items: list[Item]
    count: int


def _completion(content: str) -> dict:
    return {
        "id": "c1",
        "object": "chat.completion",
        "created": 0,
        "model": "m",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
    }


def make_llm(replies, settings: LLMSettings | None = None, **kwargs):
    requests: list[dict] = []
    queue = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        reply = queue.pop(0)
        if isinstance(reply, tuple):
            status, headers, *body = reply
            error = body[0] if body else {"message": "x"}
            return httpx.Response(status, headers=headers, json={"error": error})
        if isinstance(reply, int):
            return httpx.Response(reply, json={"error": {"message": "server error"}})
        return httpx.Response(200, json=_completion(reply))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    llm = OpenAICompatLLM(
        settings or LLMSettings(base_url="http://test/v1", model="base"),
        http_client=client,
        retry_wait=wait_none(),
        **kwargs,
    )
    return llm, requests


def test_strict_json_schema_is_strict_and_keeps_title_property():
    schema = strict_json_schema(Box)
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["items", "count"]
    item = schema["$defs"]["Item"]
    assert item["additionalProperties"] is False
    assert item["required"] == ["title", "tags"]
    assert "title" in item["properties"]  # свойство, буквально названное "title", сохраняется
    assert "title" not in item  # ключевое слово схемы "title" удаляется
    assert "default" not in item["properties"]["tags"]


def test_clean_json():
    raw = '<think>let me think</think>\n```json\n{"a": 1}\n```'
    assert clean_json(raw) == '{"a": 1}'
    assert clean_json('Sure! {"a": 2} hope this helps') == '{"a": 2}'


async def test_structured_strips_fences_and_think():
    llm, requests = make_llm(['<think>x</think>```json\n{"items": [], "count": 0}\n```'])
    box = await llm.structured([{"role": "user", "content": "go"}], Box, task="extract")
    assert box.count == 0
    rf = requests[0]["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["schema"]["additionalProperties"] is False


async def test_structured_repairs_invalid_json():
    llm, requests = make_llm(['{"items": [], "count": "many"}', '{"items": [], "count": 2}'])
    box = await llm.structured([{"role": "user", "content": "go"}], Box, task="extract")
    assert box.count == 2
    assert len(requests) == 2
    assert "not valid JSON" in requests[1]["messages"][-1]["content"]


async def test_structured_repairs_do_not_resend_unbounded_invalid_output():
    llm, requests = make_llm(["x" * 50000, "y" * 50000, '{"items": [], "count": 2}'])
    original = [{"role": "user", "content": "extract this document"}]
    try:
        result = await llm.structured(original, Box, task="extract")
        assert result.count == 2
        for request in requests[1:]:
            assert request["messages"][0] == original[0]
            assert sum(len(m["content"]) for m in request["messages"]) < 4000
    finally:
        await llm._client.close()


async def test_structured_gives_up_after_repairs():
    llm, requests = make_llm(["nope", "nope", "nope"])
    with pytest.raises(StructuredOutputError):
        await llm.structured([{"role": "user", "content": "go"}], Box, task="extract")
    assert len(requests) == 3


async def test_retries_server_errors():
    llm, requests = make_llm([500, '{"items": [], "count": 1}'])
    box = await llm.structured([{"role": "user", "content": "go"}], Box, task="extract")
    assert box.count == 1
    assert len(requests) == 2


async def test_model_override_usage_and_think_stripping():
    settings = LLMSettings(base_url="http://test/v1", model="base", overrides={"wiki": "big"})
    llm, requests = make_llm(["<think>hidden</think>Visible text"], settings)
    text = await llm.complete([{"role": "user", "content": "hi"}], task="wiki")
    assert text == "Visible text"
    assert requests[0]["model"] == "big"
    assert (llm.usage.tokens_in, llm.usage.tokens_out) == (3, 5)


async def test_retry_after_header_sets_the_pause(monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    llm, requests = make_llm([(429, {"retry-after": "7"}), "ok"])
    assert await llm.complete([{"role": "user", "content": "go"}], task="answer") == "ok"
    assert len(requests) == 2
    assert slept == [7.0]


@pytest.fixture
def clock(monkeypatch):
    """Ложные часы: `asyncio.sleep` не ждёт, а сдвигает `_monotonic` клиента; `clock.slept`."""

    class Clock:
        now = 1000.0
        slept: list[float] = []

    async def fake_sleep(seconds):
        Clock.slept.append(seconds)
        Clock.now += seconds

    Clock.slept = []
    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    monkeypatch.setattr("mnogobase.llm.client._monotonic", lambda: Clock.now)
    return Clock


async def test_rate_limit_429_is_retried_beyond_five_attempts(clock):
    llm, requests = make_llm([(429, {})] * 8 + ["ok"])
    assert await llm.complete([{"role": "user", "content": "go"}], task="resolve") == "ok"
    assert len(requests) == 9


async def test_server_errors_still_stop_after_five_attempts(clock):
    llm, requests = make_llm([500] * 6)
    with pytest.raises(openai.InternalServerError):
        await llm.complete([{"role": "user", "content": "go"}], task="resolve")
    assert len(requests) == 5


async def test_exhausted_quota_429_is_not_retried_until_deadline(clock):
    quota = (429, {}, {"message": "no money", "type": "insufficient_quota", "code": "x"})
    llm, requests = make_llm([quota] * 6)
    with pytest.raises(openai.RateLimitError):
        await llm.complete([{"role": "user", "content": "go"}], task="resolve")
    assert len(requests) == 5


async def test_rate_limit_429_stops_at_deadline_from_first_failure(clock):
    settings = LLMSettings(base_url="http://test/v1", model="m", retry_deadline_s=10)
    llm, requests = make_llm([(429, {"retry-after": "4"})] * 10, settings)
    with pytest.raises(openai.RateLimitError):
        await llm.complete([{"role": "user", "content": "go"}], task="resolve")
    # неудачи в 0, 4, 8 и 12 с; после четвёртой срок 10 с вышел
    assert len(requests) == 4
    assert clock.slept == [4.0, 4.0, 4.0]


async def test_rate_limiter_keeps_a_sliding_minute(clock):
    limiter = RateLimiter(2)
    assert await limiter.acquire() == 0
    clock.now += 30
    assert await limiter.acquire() == 0
    assert await limiter.acquire() == 30.0  # ждёт, пока первое занятие выйдет из окна
    assert await limiter.acquire() == 30.0  # второе вышло ещё через 30 с
    assert clock.slept == [30.0, 30.0]


async def test_requests_per_minute_paces_calls(clock):
    settings = LLMSettings(base_url="http://test/v1", model="m", requests_per_minute=2)
    llm, requests = make_llm(["a", "b", "c"], settings)
    for _ in range(3):
        await llm.complete([{"role": "user", "content": "go"}], task="resolve")
    assert len(requests) == 3
    assert clock.slept == [60.0]


async def test_throttle_wait_does_not_count_toward_the_retry_deadline(clock):
    settings = LLMSettings(
        base_url="http://test/v1", model="m", requests_per_minute=1, retry_deadline_s=10
    )
    llm, requests = make_llm([(429, {}), (429, {}), "ok"], settings)
    assert await llm.complete([{"role": "user", "content": "go"}], task="resolve") == "ok"
    assert len(requests) == 3
    # паузы между попытками нулевые (wait_none), 60 с — ожидание окна частоты
    assert [s for s in clock.slept if s] == [60.0, 60.0]


async def test_stream_waits_for_rate_limit_and_frees_the_slot_on_error(clock):
    settings = LLMSettings(
        base_url="http://test/v1", model="m", concurrency=1, requests_per_minute=1
    )
    llm, requests = make_streaming_llm([503, ["ok"]], settings)
    assert await _collect(llm) == ["ok"]
    assert len(requests) == 2
    assert [s for s in clock.slept if s] == [60.0]
    assert not llm._sem.locked()


async def test_failing_usage_recorder_does_not_break_the_call():
    def broken(event):
        raise RuntimeError("usage store down")

    llm, _ = make_llm(["fine"], usage_recorder=broken)
    assert await llm.complete([{"role": "user", "content": "go"}], task="answer") == "fine"


async def test_semaphore_is_free_while_waiting_between_retries(monkeypatch):
    llm, _ = make_llm([500, "ok"], LLMSettings(base_url="http://test/v1", model="m", concurrency=1))
    seen = []

    async def fake_sleep(seconds):
        seen.append(llm._sem.locked())

    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    await llm.complete([{"role": "user", "content": "go"}], task="answer")
    assert seen == [False]


def _sse(deltas: list[str], usage: tuple[int, int] | None = (3, 5)) -> bytes:
    """Поток `/chat/completions` с `stream=True`: дельты, затем чанк с usage и `[DONE]`."""
    events = []
    for text in deltas:
        chunk = {
            "id": "c1",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "m",
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
        }
        events.append(chunk)
    if usage is not None:
        events.append(
            {
                "id": "c1",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "m",
                "choices": [],
                "usage": {
                    "prompt_tokens": usage[0],
                    "completion_tokens": usage[1],
                    "total_tokens": sum(usage),
                },
            }
        )
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
    return body.encode()


def make_streaming_llm(replies, settings: LLMSettings | None = None, **kwargs):
    requests: list[dict] = []
    queue = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        reply = queue.pop(0)
        if isinstance(reply, int):
            return httpx.Response(reply, json={"error": {"message": "server error"}})
        return httpx.Response(
            200, content=_sse(reply), headers={"content-type": "text/event-stream"}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    llm = OpenAICompatLLM(
        settings or LLMSettings(base_url="http://test/v1", model="base"),
        http_client=client,
        retry_wait=wait_none(),
        **kwargs,
    )
    return llm, requests


async def _collect(llm) -> list[str]:
    return [d async for d in llm.stream([{"role": "user", "content": "go"}], task="answer")]


async def test_stream_yields_deltas_and_counts_usage():
    from mnogobase.llm.client import Usage, usage_scope

    llm, requests = make_streaming_llm([["Hel", "lo [1]"]])
    with usage_scope() as used:
        parts = await _collect(llm)
    assert "".join(parts) == "Hello [1]"
    assert llm.usage == Usage(3, 5) and used == Usage(3, 5)
    assert requests[0]["stream"] is True
    assert requests[0]["stream_options"] == {"include_usage": True}


async def test_stream_hides_think_blocks_and_leading_space():
    llm, _ = make_streaming_llm([["<thi", "nk>x</think>", "\n Answer", " here"]])
    assert "".join(await _collect(llm)) == "Answer here"


async def test_stream_keeps_text_that_only_looks_like_think():
    llm, _ = make_streaming_llm([["a <", "b> c"]])
    assert "".join(await _collect(llm)) == "a <b> c"


async def test_stream_retries_before_first_token():
    llm, requests = make_streaming_llm([503, ["ok"]])
    assert await _collect(llm) == ["ok"]
    assert len(requests) == 2


async def test_stream_records_telemetry():
    events = []
    llm, _ = make_streaming_llm([["ok"]], usage_recorder=events.append)
    await _collect(llm)
    assert [e.status for e in events] == ["started", "response"]
    assert (events[-1].tokens_in, events[-1].tokens_out) == (3, 5)


async def test_stream_retries_without_usage_option_when_provider_rejects_it():
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if "stream_options" in body:
            return httpx.Response(400, json={"error": {"message": "unknown parameter"}})
        return httpx.Response(
            200, content=_sse(["ok"], usage=None), headers={"content-type": "text/event-stream"}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    llm = OpenAICompatLLM(
        LLMSettings(base_url="http://test/v1", model="base"),
        http_client=client,
        retry_wait=wait_none(),
    )
    assert await _collect(llm) == ["ok"]
    assert await _collect(llm) == ["ok"]  # второй раз сразу без опции
    assert ["stream_options" in r for r in requests] == [True, False, False]
