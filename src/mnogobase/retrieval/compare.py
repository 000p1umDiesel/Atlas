"""Один вопрос — один режим (`run_mode`) и все режимы рядом (`compare`).

`run_mode` — общий путь `ask`: перевод вопроса (если включён `retrieval.translate`) → поиск
выбранным retriever'ом → ответ `Answerer`. `compare` прогоняет его по всем режимам и
дописывает результаты в `runs/compare.jsonl` (по строке на режим) вместе с `config_hash`,
чтобы ответы разных конфигураций можно было сравнивать между собой.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from mnogobase.core.config import Settings
from mnogobase.core.files import rotate_if_large
from mnogobase.core.models import Answer, Source
from mnogobase.retrieval.answer import Answerer
from mnogobase.retrieval.base import Retriever
from mnogobase.retrieval.translate import QueryTranslator

# при превышении compare.jsonl переносится в compare.1.jsonl
COMPARE_MAX_BYTES = 20 * 1024 * 1024


def config_hash(settings: Settings) -> str:
    """Короткий (12 hex) хеш секций конфига, влияющих на ответ: `llm`, `embedder`,
    `chunking`, `retrieval`, `graph`. Одинаковый хеш — сравнимые строки в compare.jsonl."""
    relevant = settings.model_dump_json(
        include={"llm", "embedder", "chunking", "retrieval", "graph"}
    )
    return hashlib.sha1(relevant.encode("utf-8")).hexdigest()[:12]


async def run_mode(
    question: str,
    mode: str,
    retriever: Retriever,
    answerer: Answerer,
    k: int,
    translator: QueryTranslator | None = None,
    *,
    on_sources: Callable[[list[Source]], None] | None = None,
    on_delta: Callable[[str], None] | None = None,
) -> Answer:
    """Поиск и ответ в одном режиме; `latency_ms` включает перевод + поиск + генерацию.

    С `translator` неанглийский вопрос дополнительно ищется на английском; использованные
    формулировки сохраняются в `answer.alt_queries`. Поиск синхронный и выполняется прямо в
    event loop. `on_sources` / `on_delta` — потоковый режим `Answerer.answer`."""
    start = time.perf_counter()
    alt = await translator.alt_queries(question) if translator else []
    items = retriever.retrieve(question, k, alt)
    answer = await answerer.answer(question, mode, items, on_sources=on_sources, on_delta=on_delta)
    answer.alt_queries = alt
    answer.latency_ms = int((time.perf_counter() - start) * 1000)
    return answer


def _row(answer: Answer, ts: str, config_hash: str) -> dict:
    """Строка compare.jsonl: ответ, источники, задержка и токены одного режима."""
    return {
        "ts": ts,
        "question": answer.question,
        "mode": answer.mode,
        "answer": answer.text,
        "alt_queries": answer.alt_queries,
        "sources": [s.model_dump() for s in answer.sources],
        "latency_ms": answer.latency_ms,
        "tokens_in": answer.tokens_in,
        "tokens_out": answer.tokens_out,
        "context_tokens": answer.context_tokens,
        "config_hash": config_hash,
    }


async def compare(
    question: str,
    retrievers: dict[str, Retriever],
    answerer: Answerer,
    k: int,
    runs_dir: Path,
    config_hash: str = "",
    translator: QueryTranslator | None = None,
) -> list[Answer]:
    """Отвечает на `question` во всех режимах и дописывает по строке на режим в
    `runs_dir/compare.jsonl`.

    Все строки одного запуска получают общий `ts`. Файл пишется только после всех ответов:
    если какой-то режим упал, исключение пробрасывается и в журнал ничего не попадает.
    Возвращает ответы в порядке `retrievers`.
    """
    # Режимы идут последовательно намеренно: перевод мемоизируется в `translator` только
    # после первого вызова, поэтому параллельный запуск перевёл бы вопрос по разу на режим,
    # а `latency_ms` каждого режима включал бы ожидание чужих вызовов LLM.
    answers = [
        await run_mode(question, mode, r, answerer, k, translator) for mode, r in retrievers.items()
    ]
    runs_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).isoformat()
    log = runs_dir / "compare.jsonl"
    rotate_if_large(log, COMPARE_MAX_BYTES)  # прошлые строки — в compare.1.jsonl
    with log.open("a", encoding="utf-8") as fh:
        for answer in answers:
            fh.write(json.dumps(_row(answer, ts, config_hash), ensure_ascii=False) + "\n")
    return answers
