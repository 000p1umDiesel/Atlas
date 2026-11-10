import json

import httpx
import pytest
from pydantic import BaseModel
from tenacity import wait_none

from mnogobase.config import LLMSettings
from mnogobase.llm.client import (
    OpenAICompatLLM,
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
            status, headers = reply
            return httpx.Response(status, headers=headers, json={"error": {"message": "x"}})
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


async def test_failing_usage_recorder_does_not_break_the_call():
    def broken(event):
        raise RuntimeError("usage store down")

    llm, _ = make_llm(["fine"], team_id="t", usage_recorder=broken)
    assert await llm.complete([{"role": "user", "content": "go"}], task="answer") == "fine"


async def test_semaphore_is_free_while_waiting_between_retries(monkeypatch):
    llm, _ = make_llm([500, "ok"], LLMSettings(base_url="http://test/v1", model="m", concurrency=1))
    seen = []

    async def fake_sleep(seconds):
        seen.append(llm._sem.locked())

    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    await llm.complete([{"role": "user", "content": "go"}], task="answer")
    assert seen == [False]
