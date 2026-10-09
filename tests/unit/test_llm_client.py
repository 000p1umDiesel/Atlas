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


def make_llm(replies, settings: LLMSettings | None = None):
    requests: list[dict] = []
    queue = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        reply = queue.pop(0)
        if isinstance(reply, int):
            return httpx.Response(reply, json={"error": {"message": "server error"}})
        return httpx.Response(200, json=_completion(reply))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    llm = OpenAICompatLLM(
        settings or LLMSettings(base_url="http://test/v1", model="base"),
        http_client=client,
        retry_wait=wait_none(),
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
