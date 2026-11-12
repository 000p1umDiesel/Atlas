from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from mnogobase.core.config import Settings
from mnogobase.core.files import rotate_if_large
from mnogobase.core.models import Answer
from mnogobase.retrieval.answer import Answerer
from mnogobase.retrieval.base import Retriever
from mnogobase.retrieval.translate import QueryTranslator

COMPARE_MAX_BYTES = 20 * 1024 * 1024


def config_hash(settings: Settings) -> str:
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
) -> Answer:
    """Поиск и ответ в одном режиме; `latency_ms` включает перевод + поиск + генерацию.

    С `translator` неанглийский вопрос дополнительно ищется на английском."""
    start = time.perf_counter()
    alt = await translator.alt_queries(question) if translator else []
    items = retriever.retrieve(question, k, alt)
    answer = await answerer.answer(question, mode, items)
    answer.alt_queries = alt
    answer.latency_ms = int((time.perf_counter() - start) * 1000)
    return answer


def _row(answer: Answer, ts: str, config_hash: str) -> dict:
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
    `runs_dir/compare.jsonl`."""
    # последовательно намеренно: учёт токенов по ответам читает общий счётчик usage
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
