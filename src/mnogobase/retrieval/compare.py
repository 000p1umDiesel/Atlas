from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from mnogobase.config import Settings
from mnogobase.models import Answer
from mnogobase.retrieval.answer import Answerer
from mnogobase.retrieval.base import Retriever


def config_hash(settings: Settings) -> str:
    relevant = settings.model_dump_json(
        include={"llm", "embedder", "chunking", "retrieval", "graph"}
    )
    return hashlib.sha1(relevant.encode("utf-8")).hexdigest()[:12]


async def run_mode(
    question: str, mode: str, retriever: Retriever, answerer: Answerer, k: int
) -> Answer:
    """Retrieve and answer in one mode; `latency_ms` covers retrieval + generation."""
    start = time.perf_counter()
    items = retriever.retrieve(question, k)
    answer = await answerer.answer(question, mode, items)
    answer.latency_ms = int((time.perf_counter() - start) * 1000)
    return answer


def _row(answer: Answer, ts: str, config_hash: str) -> dict:
    return {
        "ts": ts,
        "question": answer.question,
        "mode": answer.mode,
        "answer": answer.text,
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
) -> list[Answer]:
    """Answer `question` in every mode and append one row per mode to `runs_dir/compare.jsonl`."""
    # sequential on purpose: per-answer token accounting reads the shared usage counter
    answers = [await run_mode(question, mode, r, answerer, k) for mode, r in retrievers.items()]
    runs_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).isoformat()
    with (runs_dir / "compare.jsonl").open("a", encoding="utf-8") as fh:
        for answer in answers:
            fh.write(json.dumps(_row(answer, ts, config_hash), ensure_ascii=False) + "\n")
    return answers
