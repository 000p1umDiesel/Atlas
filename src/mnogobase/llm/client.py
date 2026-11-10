from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Protocol, TypeVar

import httpx
import openai
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
    stop_after_delay,
    wait_exponential,
)

from mnogobase.config import LLMSettings
from mnogobase.llm.telemetry import CallEvent, new_call, record_call
from mnogobase.log import get_logger

T = TypeVar("T", bound=BaseModel)
Message = dict[str, str]

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
_RETRYABLE = (
    openai.APIConnectionError,  # включает APITimeoutError
    openai.RateLimitError,
    openai.InternalServerError,
)
_DEFAULT_RETRY_WAIT = wait_exponential(multiplier=1, max=30)
_MAX_RETRY_AFTER_S = 120.0


def _retry_after(exc: BaseException | None) -> float | None:
    """Секунды из заголовка `Retry-After` ответа 429/503 (только число секунд)."""
    response = getattr(exc, "response", None)
    value = response.headers.get("retry-after") if response is not None else None
    try:
        seconds = float(value) if value is not None else None
    except ValueError:
        return None
    return min(max(seconds, 0.0), _MAX_RETRY_AFTER_S) if seconds is not None else None


class StructuredOutputError(RuntimeError):
    """Модель не вернула JSON, соответствующий схеме, после всех попыток исправления."""


@dataclass
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0


class LLMClient(Protocol):
    usage: Usage

    def model_for(self, task: str) -> str: ...

    async def _record(self, event: CallEvent | None) -> None:
        # Учёт расхода не должен ломать сам вызов LLM и подменять его исключение.
        try:
            await record_call(event, self._usage_recorder)
        except Exception as exc:
            self._log.warning("llm_usage_record_failed", error_type=type(exc).__name__)

    async def complete(self, messages: list[Message], *, task: str) -> str: ...

    async def structured(
        self, messages: list[Message], schema: type[T], *, task: str, max_repairs: int = 2
    ) -> T: ...


def strip_think(text: str) -> str:
    return _THINK.sub("", text).strip()


def clean_json(text: str) -> str:
    text = _FENCE.sub("", strip_think(text)).strip()
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


def _strictify(node: Any) -> Any:
    if isinstance(node, list):
        return [_strictify(x) for x in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in ("default", "title"):
            continue
        if key in ("properties", "$defs"):
            out[key] = {name: _strictify(sub) for name, sub in value.items()}
        else:
            out[key] = _strictify(value)
    if out.get("type") == "object" and "properties" in out:
        out["additionalProperties"] = False
        out["required"] = list(out["properties"])
    return out


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic-схема → strict-схема structured output для OpenAI."""
    return _strictify(model.model_json_schema())


class OpenAICompatLLM:
    """Работает с любым OpenAI-совместимым endpoint: OpenAI, llama.cpp server, vLLM, LM Studio, прокси."""

    def __init__(
        self,
        settings: LLMSettings,
        http_client: httpx.AsyncClient | None = None,
        retry_wait=_DEFAULT_RETRY_WAIT,
        *,
        team_id: str | None = None,
        usage_recorder: Callable[[CallEvent], None] | None = None,
    ):
        self._team_id = team_id
        self._usage_recorder = usage_recorder
        self._settings = settings
        self._client = AsyncOpenAI(
            base_url=settings.url,
            api_key=settings.api_key(),
            timeout=settings.timeout_s,
            max_retries=0,  # повторами управляет tenacity
            http_client=http_client,
        )
        self._sem = asyncio.Semaphore(settings.concurrency)
        self._retry_wait = retry_wait
        self.usage = Usage()
        self._log = get_logger(__name__)

    def model_for(self, task: str) -> str:
        return self._settings.model_for(task)

    async def aclose(self) -> None:
        await self._client.close()

    async def _chat(
        self, messages: list[Message], *, task: str, response_format: dict | None = None
    ) -> str:
        model = self.model_for(task)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": self._settings.temperature,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        start = time.perf_counter()
        attempts = 0

        def wait(state: RetryCallState) -> float:
            backoff = self._retry_wait(state)
            hint = _retry_after(state.outcome.exception() if state.outcome else None)
            return max(backoff, hint) if hint is not None else backoff

        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type(_RETRYABLE),
            stop=stop_after_attempt(5) | stop_after_delay(self._settings.retry_deadline_s),
            wait=wait,
            reraise=True,
        ):
            with attempt:
                # Слот конкурентности занят только на время запроса, не на паузу между попытками.
                async with self._sem:
                    attempts += 1
                    event = new_call(
                        model, task, external=self._settings.serve == "api", team_id=self._team_id
                    )
                    await self._record(event)
                    try:
                        response = await self._client.chat.completions.create(**kwargs)
                    except BaseException:
                        if event is not None:
                            await self._record(replace(event, status="error"))
                        raise
                    if event is not None:
                        await self._record(
                            replace(
                                event,
                                status="response",
                                tokens_in=response.usage.prompt_tokens if response.usage else None,
                                tokens_out=response.usage.completion_tokens
                                if response.usage
                                else None,
                            )
                        )
        usage = response.usage
        tokens_in = usage.prompt_tokens if usage else 0
        tokens_out = usage.completion_tokens if usage else 0
        self.usage.tokens_in += tokens_in
        self.usage.tokens_out += tokens_out
        self._log.info(
            "llm_call",
            task=task,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            retries=attempts - 1,
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
        return response.choices[0].message.content or ""

    async def _record(self, event: CallEvent | None) -> None:
        # Учёт расхода не должен ломать сам вызов LLM и подменять его исключение.
        try:
            await record_call(event, self._usage_recorder)
        except Exception as exc:
            self._log.warning("llm_usage_record_failed", error_type=type(exc).__name__)

    async def complete(self, messages: list[Message], *, task: str) -> str:
        return strip_think(await self._chat(messages, task=task))

    async def structured(
        self, messages: list[Message], schema: type[T], *, task: str, max_repairs: int = 2
    ) -> T:
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": schema.__name__,
                "schema": strict_json_schema(schema),
                "strict": True,
            },
        }
        history = list(messages)
        last_error = ""
        for _ in range(max_repairs + 1):
            content = await self._chat(history, task=task, response_format=response_format)
            try:
                return schema.model_validate_json(clean_json(content))
            except ValidationError as exc:  # возникает и для некорректного JSON
                last_error = str(exc)
                self._log.warning("llm_invalid_json", task=task, error=last_error[:300])
                history = [
                    *messages,
                    # A truncated/looping reply can fill the model's whole
                    # context. Keep only its beginning and the latest error.
                    {"role": "assistant", "content": content[:2000]},
                    {
                        "role": "user",
                        "content": "Your previous reply was not valid JSON for the schema: "
                        f"{last_error[:500]}\nReply again with only the corrected JSON.",
                    },
                ]
        raise StructuredOutputError(f"{schema.__name__}: {last_error[:500]}")
