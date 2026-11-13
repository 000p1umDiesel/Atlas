"""Генерация ответа по найденному контексту с нумерованными ссылками на источники.

Последний шаг `ask`: элементы `ContextItem` от retriever'а превращаются в блоки
`[n] (метка)\\nтекст`, обрезаются по бюджету `retrieval.context_tokens`, подставляются в
промпт `answer.md` (вопрос и контекст — в `user`-сообщении как недоверенные данные), и LLM с
`task="answer"` пишет ответ, ссылаясь на блоки как `[n]`. Номера, которые встретились в тексте
ответа, помечают соответствующие `Source.cited`.

Если контекст пуст, LLM не вызывается: возвращается фиксированный `NO_SOURCES`.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from pathlib import Path

from mnogobase.core.config import LANGUAGE_NAMES
from mnogobase.core.models import Answer, ContextItem, Source
from mnogobase.llm.client import LLMClient, strip_think, usage_scope
from mnogobase.llm.templates import render_messages
from mnogobase.retrieval.base import estimate_tokens

NO_SOURCES = "В базе знаний нет подходящих источников для этого вопроса."
# ссылка на источник в тексте ответа: `[3]`
_CITED = re.compile(r"\[(\d+)\]")


def _label(item: ContextItem) -> str:
    """Короткая метка блока для LLM: имя файла и страница для чанка, путь для wiki, вид для
    элементов графа (`graph entity` / `graph relation`)."""
    if item.kind == "chunk":
        where = Path(item.path).name if item.path else item.ref
        return f"document {where}" + (f", p.{item.page}" if item.page is not None else "")
    if item.kind == "wiki":
        return f"wiki {item.path or item.ref}"
    return f"graph {item.kind}"


def _context_blocks(
    items: list[ContextItem], max_tokens: int
) -> tuple[list[str], list[Source], int]:
    """Нумерует элементы блоками `[n] (label)\\ntext`, пока не исчерпан бюджет токенов.

    Элементы берутся по порядку; на первом, который не помещается, перебор останавливается
    (в отличие от `CombinedRetriever`, здесь меньшие элементы после него не добираются —
    порядок от retriever'а важнее). Первый элемент сохраняется всегда, даже сверх бюджета,
    чтобы непустой поиск никогда не давал пустой контекст.

    Возвращает (блоки, источники с теми же номерами `n`, оценку израсходованных токенов).
    """
    blocks: list[str] = []
    sources: list[Source] = []
    used = 0
    for item in items:
        cost = estimate_tokens(item.text)
        if sources and used + cost > max_tokens:
            break
        n = len(sources) + 1
        blocks.append(f"[{n}] ({_label(item)})\n{item.text}")
        sources.append(
            Source(
                n=n,
                kind=item.kind,
                ref=item.ref,
                path=item.path,
                page=item.page,
                snippet=item.text[:200],
            )
        )
        used += cost
    return blocks, sources, used


class Answerer:
    """Отвечает на вопрос по готовому контексту; один экземпляр обслуживает все режимы."""

    def __init__(self, llm: LLMClient, max_context_tokens: int, language: str = "auto"):
        """`max_context_tokens` — бюджет контекста (`retrieval.context_tokens`);
        `language` — код вроде `ru` / `en` (или название языка), на котором писать ответ;
        `auto` — на языке вопроса."""
        self._llm = llm
        self._max = max_context_tokens
        self._language = (
            "the language of the question"
            if language == "auto"
            else LANGUAGE_NAMES.get(language, language)
        )

    async def answer(
        self,
        question: str,
        mode: str,
        items: list[ContextItem],
        *,
        on_sources: Callable[[list[Source]], None] | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> Answer:
        """Строит контекст из `items`, вызывает LLM и возвращает `Answer`.

        В `Answer` попадают текст, источники с флагом `cited`, сами блоки контекста (`contexts`,
        нужны для оценки Ragas), токены этого вызова и `latency_ms` только генерации (поиск и
        перевод сюда не входят; полное время считает `compare.run_mode`). Ошибки LLM
        пробрасываются как есть.

        Потоковый режим (веб-чат): `on_sources(sources)` вызывается до генерации (флаги
        `cited` ещё не выставлены), `on_delta(text)` — на каждый кусок ответа; без контекста
        ни один из них не вызывается.
        """
        start = time.perf_counter()
        blocks, sources, context_tokens = _context_blocks(items, self._max)
        context = "\n\n".join(blocks)
        if not sources:
            return Answer(
                question=question,
                mode=mode,
                text=NO_SOURCES,
                latency_ms=int((time.perf_counter() - start) * 1000),
            )
        messages = render_messages(
            "answer", question=question, context=context, language=self._language
        )
        # usage_scope считает токены только этого вызова, а не параллельных вопросов
        # на том же клиенте
        if on_sources is not None:
            on_sources(sources)
        with usage_scope() as used:
            if on_delta is None:
                text = await self._llm.complete(messages, task="answer")
            else:
                parts = []
                async for part in self._llm.stream(messages, task="answer"):
                    parts.append(part)
                    on_delta(part)
                text = strip_think("".join(parts))
        cited = {int(n) for n in _CITED.findall(text)}
        for source in sources:
            source.cited = source.n in cited
        return Answer(
            question=question,
            mode=mode,
            text=text,
            sources=sources,
            contexts=blocks,
            latency_ms=int((time.perf_counter() - start) * 1000),
            tokens_in=used.tokens_in,
            tokens_out=used.tokens_out,
            context_tokens=context_tokens,
        )
