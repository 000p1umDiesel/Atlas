"""Ragas adapter; importing this module does not construct clients or load Ragas."""

from __future__ import annotations

import math
import os

from mnogobase.config import Settings

METRIC_NAMES = (
    "faithfulness",
    "answer_relevancy",
    "context_precision_with_reference",
    "context_recall",
    "factual_correctness",
)
REFERENCE_METRICS = {"context_precision_with_reference", "context_recall", "factual_correctness"}
CONTEXT_METRICS = {"faithfulness", "context_precision_with_reference", "context_recall"}


def judge_config(settings: Settings) -> dict:
    s = settings.evaluation
    return {
        "base_url": s.base_url or settings.llm.url,
        "model": s.model or settings.llm.model_for("answer"),
        "api_key_env": s.api_key_env if s.api_key_env is not None else settings.llm.api_key_env,
        "embedding_url": s.embedding_url or settings.embedder.url,
        "embedding_model": s.embedding_model or settings.embedder.model,
        "embedding_api_key_env": (
            s.embedding_api_key_env
            if s.embedding_api_key_env is not None
            else settings.embedder.api_key_env
        ),
        "timeout_s": s.timeout_s,
        "temperature": s.temperature,
        "strictness": s.strictness,
    }


def api_key(env_name: str | None) -> str:
    if env_name and not os.environ.get(env_name):
        raise ValueError(f"Set {env_name} for the evaluation endpoint")
    return os.environ[env_name] if env_name else "local"


def skip_reason(row: dict, name: str) -> str | None:
    if row.get("status") != "ok":
        return "inference_failed_or_missing"
    if name in REFERENCE_METRICS and not (row.get("reference") or "").strip():
        return "no_reference"
    if name in CONTEXT_METRICS and not row.get("contexts"):
        return "empty_context"
    return None


def authentication_error(exc: Exception) -> bool:
    pending, seen = [exc], set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if getattr(current, "status_code", None) in (401, 403):
            return True
        pending.extend([current.__cause__, current.__context__])
        pending.extend(a.exception for a in getattr(current, "failed_attempts", []) or [])
    return False


async def evaluate_one(row: dict, name: str, metric) -> dict:
    if name not in METRIC_NAMES:
        raise ValueError(f"Unknown metric: {name}")
    reason = skip_reason(row, name)
    if reason:
        return {"status": "skipped", "reason": reason, "value": None}
    kwargs = {}
    if name != "factual_correctness":
        kwargs["user_input"] = row["question"]
    if name in ("faithfulness", "answer_relevancy", "factual_correctness"):
        kwargs["response"] = row["text"]
    if name in REFERENCE_METRICS:
        kwargs["reference"] = row["reference"]
    if name in CONTEXT_METRICS:
        kwargs["retrieved_contexts"] = row["contexts"]
    try:
        value = float((await metric.ascore(**kwargs)).value)
        if not math.isfinite(value):
            return {"status": "undefined", "reason": "nonfinite_metric", "value": None}
        return {"status": "ok", "value": value}
    except Exception as exc:
        # Do not persist API exception messages: they can contain request headers/secrets.
        return {
            "status": "error",
            "error": type(exc).__name__,
            "value": None,
            "reason": "authentication_error" if authentication_error(exc) else "metric_failed",
        }


class RagasJudge:
    def __init__(self, config: dict):
        self.config = config
        self.clients = []

    def metrics(self, names) -> dict:
        from openai import AsyncOpenAI
        from ragas.embeddings.base import embedding_factory
        from ragas.llms import llm_factory
        from ragas.metrics.collections import (
            AnswerRelevancy,
            ContextPrecisionWithReference,
            ContextRecall,
            FactualCorrectness,
            Faithfulness,
        )

        c = self.config
        client = AsyncOpenAI(
            base_url=c["base_url"],
            api_key=api_key(c["api_key_env"]),
            timeout=c["timeout_s"],
            max_retries=2,
        )
        self.clients.append(client)
        llm = llm_factory(c["model"], client=client, temperature=c["temperature"])
        constructors = {
            "faithfulness": Faithfulness,
            "context_precision_with_reference": ContextPrecisionWithReference,
            "context_recall": ContextRecall,
            "factual_correctness": lambda **kw: FactualCorrectness(**kw, mode="f1"),
        }
        result = {name: constructors[name](llm=llm) for name in names if name != "answer_relevancy"}
        if "answer_relevancy" in names:
            embed_client = AsyncOpenAI(
                base_url=c["embedding_url"],
                api_key=api_key(c["embedding_api_key_env"]),
                timeout=c["timeout_s"],
                max_retries=2,
            )
            self.clients.append(embed_client)
            embeddings = embedding_factory(
                "openai", model=c["embedding_model"], client=embed_client
            )
            result["answer_relevancy"] = AnswerRelevancy(
                llm=llm, embeddings=embeddings, strictness=c["strictness"]
            )
        return result

    async def close(self):
        for client in self.clients:
            await client.close()
