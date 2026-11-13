"""Клиент генеративной LLM: протокол `LLMClient` и реализация `OpenAICompatLLM`.

Через этот модуль идут все обращения к `/chat/completions`: извлечение сущностей
(`extract`), проверка слияний (`resolve`), страницы wiki (`wiki`), ответы (`answer`) и
перевод/переписывание вопросов (`query`).

Выбор модели по задаче. Каждый вызов передаёт `task` из `LLM_TASKS`; `model_for(task)`
возвращает `llm.overrides[task]`, если он задан, иначе `llm.model`. Так отдельную задачу
можно отдать другой модели того же сервера, не трогая остальные.

Повторы. Сам SDK OpenAI не повторяет (`max_retries=0`), повторами управляет tenacity:
повторяются только сетевые ошибки и таймауты, 429 и 5xx (`_RETRYABLE`); не больше 5 попыток
и не дольше `llm.retry_deadline_s` суммарно. Пауза — экспоненциальная (до 30 с), но не
меньше `Retry-After` из ответа сервера (не больше `_MAX_RETRY_AFTER_S`). Семафор
`llm.concurrency` ограничивает число одновременных запросов и занят только на время самого
запроса, не на паузу между попытками.

Структурированный ответ с исправлением. `structured` просит JSON по strict-схеме, которую
строит из pydantic-модели (`strict_json_schema`); схема ответа задаётся в коде, а не в
промпте. Ответ очищается от `<think>…</think>` и markdown-ограды и валидируется моделью;
при ошибке модели показывают начало её ответа и текст ошибки и просят исправить — до
`max_repairs` раз, после чего бросается `StructuredOutputError`.

Учёт токенов. Каждый успешный вызов передаёт токены в `count_usage`: они добавляются к
общему счётчику клиента `client.usage` и ко всем открытым `usage_scope` текущей
asyncio-задачи (ContextVar), так что параллельные вопросы на одном клиенте считают только
свои токены. Отдельно каждая попытка (без текста промптов) отправляется в `usage_recorder`
(`telemetry.CallEvent` → `llm_usage` в PostgreSQL); сбой записи учёта никогда не ломает сам
вызов.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
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

from mnogobase.core.config import LLMSettings
from mnogobase.core.log import get_logger
from mnogobase.llm.telemetry import CallEvent, new_call, record_call

T = TypeVar("T", bound=BaseModel)
# сообщение чата в формате OpenAI: {"role": "system" | "user" | "assistant", "content": ...}
Message = dict[str, str]

# рассуждения reasoning-моделей (Qwen3 и т. п.), которые не должны попасть в ответ или JSON
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
_RETRYABLE = (
    openai.APIConnectionError,  # включает APITimeoutError
    openai.RateLimitError,
    openai.InternalServerError,
)
_DEFAULT_RETRY_WAIT = wait_exponential(multiplier=1, max=30)
# верхняя граница подсказки сервера: слишком большой Retry-After не должен подвесить вызов
_MAX_RETRY_AFTER_S = 120.0


def _retry_after(exc: BaseException | None) -> float | None:
    """Секунды из заголовка `Retry-After` ответа 429/503 (только число секунд).

    Форма с HTTP-датой не поддерживается и даёт `None`, как и отсутствие ответа или
    заголовка. Значение ограничивается диапазоном [0, `_MAX_RETRY_AFTER_S`]."""
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
    """Счётчик токенов: входные (prompt) и выходные (completion)."""

    tokens_in: int = 0
    tokens_out: int = 0


# Открытые `usage_scope` текущей asyncio-задачи. ContextVar копируется в каждую задачу,
# поэтому параллельные вопросы на одном клиенте (API) считают только свои вызовы.
_USAGE_SCOPES: ContextVar[tuple[Usage, ...]] = ContextVar("llm_usage_scopes", default=())


@contextmanager
def usage_scope() -> Iterator[Usage]:
    """Счётчик токенов всех вызовов LLM внутри блока `with` в текущей задаче.

    Разница общего `client.usage` до и после вызова тут не годится: пока задача ждёт ответа,
    тот же клиент успевает обслужить вызовы других задач. Области вложенные: вызов
    учитывается во всех открытых областях сразу. Задачи, созданные внутри блока
    (`asyncio.gather` и т. п.), получают копию контекста и тоже пишут в эту область."""
    scope = Usage()
    token = _USAGE_SCOPES.set((*_USAGE_SCOPES.get(), scope))
    try:
        yield scope
    finally:
        _USAGE_SCOPES.reset(token)


def count_usage(total: Usage, tokens_in: int, tokens_out: int) -> None:
    """Добавляет токены одного вызова LLM к общему счётчику клиента `total` и ко всем
    открытым `usage_scope` текущей задачи. Вызывает каждая реализация `LLMClient`."""
    for usage in (total, *_USAGE_SCOPES.get()):
        usage.tokens_in += tokens_in
        usage.tokens_out += tokens_out


class LLMClient(Protocol):
    """Интерфейс генеративной LLM; тесты подставляют `FakeLLM` из `tests/fakes.py`.

    `task` — одно из `LLM_TASKS` (extract / resolve / wiki / answer / query); по нему
    реализация выбирает модель. `usage` — накопленный счётчик токенов клиента; реализация
    обязана вызывать `count_usage` на каждый вызов, чтобы работали `usage_scope`.
    """

    usage: Usage

    def model_for(self, task: str) -> str:
        """Имя модели, которой уйдёт задача `task`."""
        ...

    async def complete(self, messages: list[Message], *, task: str) -> str:
        """Текстовый ответ на `messages` без блоков `<think>`."""
        ...

    async def structured(
        self, messages: list[Message], schema: type[T], *, task: str, max_repairs: int = 2
    ) -> T:
        """Ответ, провалидированный как `schema`; после `max_repairs` неудачных попыток
        исправления — `StructuredOutputError`."""
        ...

    def stream(self, messages: list[Message], *, task: str) -> AsyncIterator[str]:
        """Текстовый ответ кусками по мере генерации, без блоков `<think>` (склеенные куски
        после `strip_think` совпадают с `complete`). Токены учитываются в конце потока."""
        ...


def strip_think(text: str) -> str:
    """Убирает блоки `<think>…</think>` и пробелы по краям."""
    return _THINK.sub("", text).strip()


class ThinkFilter:
    """Потоковый аналог `strip_think`: убирает `<think>…</think>` из текста, приходящего
    кусками, и пробелы в начале ответа.

    Хвост куска, который может оказаться началом тега (`<thi`), придерживается до
    следующего куска, поэтому тег, разрезанный между кусками, тоже распознаётся."""

    _OPEN, _CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self._buffer, self._inside, self._started = "", False, False

    def feed(self, text: str) -> str:
        """Принимает очередной кусок и возвращает то, что уже можно показать."""
        self._buffer += text
        out: list[str] = []
        while True:
            lowered = self._buffer.lower()
            if self._inside:
                end = lowered.find(self._CLOSE)
                if end < 0:
                    self._buffer = self._buffer[-(len(self._CLOSE) - 1) :]
                    break
                self._buffer, self._inside = self._buffer[end + len(self._CLOSE) :], False
                continue
            start = lowered.find(self._OPEN)
            if start >= 0:
                out.append(self._buffer[:start])
                self._buffer, self._inside = self._buffer[start + len(self._OPEN) :], True
                continue
            keep = next(
                (n for n in range(len(self._OPEN) - 1, 0, -1) if lowered.endswith(self._OPEN[:n])),
                0,
            )
            out.append(self._buffer[: len(self._buffer) - keep])
            self._buffer = self._buffer[len(self._buffer) - keep :]
            break
        return self._visible("".join(out))

    def flush(self) -> str:
        """Остаток после конца потока (незакрытый `<think>` отбрасывается)."""
        rest = "" if self._inside else self._buffer
        self._buffer = ""
        return self._visible(rest)

    def _visible(self, text: str) -> str:
        """Срезает пробелы до первого видимого символа ответа."""
        if not self._started:
            text = text.lstrip()
            self._started = bool(text)
        return text


def clean_json(text: str) -> str:
    """Достаёт JSON-объект из ответа модели: без `<think>`, без markdown-ограды ```json и
    без текста вокруг (от первой `{` до последней `}`). Если скобок нет, возвращает текст
    как есть — ошибку тогда покажет валидация."""
    text = _FENCE.sub("", strip_think(text)).strip()
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


def _strictify(node: Any) -> Any:
    """Рекурсивно приводит JSON Schema к требованиям strict structured output OpenAI.

    Убирает `default` и `title`, а каждому объекту с `properties` ставит
    `additionalProperties: false` и делает обязательными все поля (strict-режим не допускает
    необязательных ключей). Ключи внутри `properties` / `$defs` — имена полей и моделей, а не
    ключевые слова схемы, поэтому они не фильтруются (поле может называться `title`).
    """
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
    """Работает с любым OpenAI-совместимым endpoint: OpenAI, llama.cpp server, LM Studio, прокси.

    Привязан к event loop, в котором создан (асинхронный клиент и семафор); в API поэтому
    живёт в отдельном потоке `CoreLoop`. Закрывать через `aclose()`.
    """

    def __init__(
        self,
        settings: LLMSettings,
        http_client: httpx.AsyncClient | None = None,
        retry_wait=_DEFAULT_RETRY_WAIT,
        *,
        usage_recorder: Callable[[CallEvent], None] | None = None,
    ):
        """`http_client` — свой httpx-клиент (тесты, прокси); `retry_wait` — стратегия паузы
        tenacity между повторами; `usage_recorder` — синхронная функция записи
        `CallEvent` (в `build_app` — `UsageStore.record`), вызывается в отдельном потоке."""
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
        # False после 400 на `stream_options`: такой сервер не принимает запрос токенов в потоке
        self._stream_usage = True
        self._retry_wait = retry_wait
        self.usage = Usage()
        self._log = get_logger(__name__)

    def model_for(self, task: str) -> str:
        """`llm.overrides[task]`, если задан, иначе `llm.model`."""
        return self._settings.model_for(task)

    async def aclose(self) -> None:
        """Закрывает HTTP-клиент OpenAI SDK."""
        await self._client.close()

    async def _chat(
        self, messages: list[Message], *, task: str, response_format: dict | None = None
    ) -> str:
        """Один логический вызов `/chat/completions` с повторами; возвращает сырой текст.

        Каждая попытка — отдельный `CallEvent`: сначала `started`, затем `response` с токенами
        или `error`. После успеха токены идут в `count_usage`, в лог пишется `llm_call`.
        Неповторяемые ошибки и последняя ошибка после исчерпания попыток пробрасываются.
        """
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
            # экспоненциальная пауза, но не короче, чем просит сервер в Retry-After
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
                    # external: модель за API (`serve: api`), а не локальный llama-server;
                    # только к таким вызовам применяются тарифы `service.llm_rates`
                    event = new_call(model, task, external=self._settings.serve == "api")
                    await self._record(event)
                    try:
                        response = await self._client.chat.completions.create(**kwargs)
                    except BaseException:
                        # BaseException: отмена задачи (например, по таймауту) тоже
                        # закрывает попытку статусом error, а не оставляет её started
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
        count_usage(self.usage, tokens_in, tokens_out)
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
        """Передаёт событие попытки в `usage_recorder`, проглатывая его ошибки."""
        # Учёт расхода не должен ломать сам вызов LLM и подменять его исключение.
        try:
            await record_call(event, self._usage_recorder)
        except Exception as exc:
            self._log.warning("llm_usage_record_failed", error_type=type(exc).__name__)

    async def complete(self, messages: list[Message], *, task: str) -> str:
        """Текстовый ответ модели для задачи `task` без блоков `<think>`."""
        return strip_think(await self._chat(messages, task=task))

    async def stream(self, messages: list[Message], *, task: str) -> AsyncIterator[str]:
        """Ответ кусками (`stream=True`) без блоков `<think>`.

        Повторы (как у `_chat`) возможны только до первого куска: оборванный посреди ответ
        не повторяется, ошибка пробрасывается. Слот конкурентности держится от успешного
        открытия потока до его конца, но не на паузах между попытками. Токены приходят
        последним куском (`stream_options.include_usage`); сервер без него даёт 0. Если
        потребитель бросил поток (закрыл генератор), попытка записывается как `error`."""
        model = self.model_for(task)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": self._settings.temperature,
            "stream": True,
        }
        if self._stream_usage:
            kwargs["stream_options"] = {"include_usage": True}
        start = time.perf_counter()
        attempts = 0
        event: CallEvent | None = None

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
                await self._sem.acquire()
                attempts += 1
                event = new_call(model, task, external=self._settings.serve == "api")
                await self._record(event)
                try:
                    try:
                        response = await self._client.chat.completions.create(**kwargs)
                    except openai.BadRequestError:
                        # Часть OpenAI-совместимых прокси отвергает незнакомый параметр:
                        # повторяем без него (токены потока тогда не учитываются) и больше
                        # его не шлём.
                        if "stream_options" not in kwargs:
                            raise
                        kwargs.pop("stream_options")
                        self._stream_usage = False
                        self._log.warning("llm_stream_usage_unsupported", model=model)
                        response = await self._client.chat.completions.create(**kwargs)
                except BaseException:
                    self._sem.release()
                    if event is not None:
                        await self._record(replace(event, status="error"))
                    raise
        usage = None
        thinking = ThinkFilter()
        try:
            async for chunk in response:
                if chunk.usage is not None:
                    usage = chunk.usage
                if chunk.choices and chunk.choices[0].delta.content:
                    visible = thinking.feed(chunk.choices[0].delta.content)
                    if visible:
                        yield visible
            tail = thinking.flush()
            if tail:
                yield tail
        except BaseException:
            if event is not None:
                await self._record(replace(event, status="error"))
            raise
        finally:
            self._sem.release()
            await response.close()
        tokens_in = usage.prompt_tokens if usage else 0
        tokens_out = usage.completion_tokens if usage else 0
        if event is not None:
            await self._record(
                replace(
                    event,
                    status="response",
                    tokens_in=usage.prompt_tokens if usage else None,
                    tokens_out=usage.completion_tokens if usage else None,
                )
            )
        count_usage(self.usage, tokens_in, tokens_out)
        self._log.info(
            "llm_call",
            task=task,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            retries=attempts - 1,
            duration_ms=int((time.perf_counter() - start) * 1000),
            stream=True,
        )

    async def structured(
        self, messages: list[Message], schema: type[T], *, task: str, max_repairs: int = 2
    ) -> T:
        """Ответ модели как экземпляр `schema` (strict JSON Schema + цикл исправления).

        Алгоритм: запрос с `response_format=json_schema` → `clean_json` → валидация. При
        `ValidationError` (в том числе для невалидного JSON) пишется warning `llm_invalid_json`,
        и следующий запрос — исходные `messages` + начало неудачного ответа (assistant) +
        текст ошибки с просьбой исправить (user). Каждая попытка исправления строится от
        исходных сообщений, а не накапливает историю. Всего не больше `max_repairs + 1`
        запросов; затем — `StructuredOutputError`. Ошибки транспорта пробрасываются из
        `_chat` без исправлений.
        """
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
                    # Обрезанный или зациклившийся ответ может занять весь контекст модели.
                    # Оставляем только его начало и последнюю ошибку.
                    {"role": "assistant", "content": content[:2000]},
                    {
                        "role": "user",
                        "content": "Your previous reply was not valid JSON for the schema: "
                        f"{last_error[:500]}\nReply again with only the corrected JSON.",
                    },
                ]
        raise StructuredOutputError(
            f"ответ модели не соответствует схеме {schema.__name__}: {last_error[:500]}"
        )
