from __future__ import annotations

import re
import time
from pathlib import Path

from mnogobase.core.config import LANGUAGE_NAMES
from mnogobase.core.models import Answer, ContextItem, Source
from mnogobase.llm.client import LLMClient, usage_scope
from mnogobase.llm.templates import render_messages
from mnogobase.retrieval.base import estimate_tokens

NO_SOURCES = "The knowledge base has no relevant sources for this question."
_CITED = re.compile(r"\[(\d+)\]")


def _label(item: ContextItem) -> str:
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

    Первый элемент сохраняется всегда, даже сверх бюджета, чтобы непустой поиск никогда
    не давал пустой контекст.
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
    def __init__(self, llm: LLMClient, max_context_tokens: int, language: str = "auto"):
        """`language`: код вроде `ru` / `en` (или название языка); `auto` — язык вопроса."""
        self._llm = llm
        self._max = max_context_tokens
        self._language = (
            "the language of the question"
            if language == "auto"
            else LANGUAGE_NAMES.get(language, language)
        )

    async def answer(self, question: str, mode: str, items: list[ContextItem]) -> Answer:
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
        with usage_scope() as used:  # только этот вызов, а не параллельные вопросы
            text = await self._llm.complete(messages, task="answer")
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
