from __future__ import annotations

import math
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

import httpx
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

from mnogobase.config import RerankSettings
from mnogobase.log import get_logger

_DEFAULT_RETRY_WAIT = wait_exponential(multiplier=1, max=20)

# Собственный формат Qwen3-Reranker: score — это P("yes") для следующего токена после пустого
# think.
_PREFIX = (
    "<|im_start|>system\nJudge whether the Document meets the requirements based on the Query "
    'and the Instruct provided. Note that the answer can only be "yes" or "no".<|im_end|>\n'
    "<|im_start|>user\n"
)
_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"


class Reranker(Protocol):
    def score(self, query: str, documents: Sequence[str]) -> list[float]:
        """Релевантность каждого документа к `query` в [0, 1], в порядке входа."""
        ...


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500


def yes_probability(top_logprobs: list[dict]) -> float:
    """P(yes) / (P(yes) + P(no)) по кандидатам первого токена (любой регистр, любые пробелы)."""
    p = {"yes": 0.0, "no": 0.0}
    for t in top_logprobs:
        word = t.get("token", "").strip().casefold()
        if word in p:
            p[word] += math.exp(t["logprob"])
    total = p["yes"] + p["no"]
    return p["yes"] / total if total else 0.0


class OllamaReranker:
    """Qwen3-Reranker через Ollama `/api/generate` (raw-промпт, один токен, его logprobs).

    В Ollama нет rerank endpoint, поэтому каждый документ — отдельный generate-запрос;
    `concurrency` из них идут параллельно (Ollama обслуживает до `OLLAMA_NUM_PARALLEL`
    одновременно).
    """

    def __init__(
        self,
        settings: RerankSettings,
        client: httpx.Client | None = None,
        retry_wait=_DEFAULT_RETRY_WAIT,
    ):
        self._s = settings
        self._client = client or httpx.Client(base_url=settings.base_url, timeout=120)
        self._retry_wait = retry_wait
        self._log = get_logger(__name__)

    def _prompt(self, query: str, document: str) -> str:
        body = (
            f"<Instruct>: {self._s.instruction}\n<Query>: {query}\n"
            f"<Document>: {document[: self._s.max_chars]}"
        )
        return _PREFIX + body + _SUFFIX

    def _score_one(self, query: str, document: str) -> float:
        payload = {
            "model": self._s.model,
            "prompt": self._prompt(query, document),
            "raw": True,
            "stream": False,
            "logprobs": True,
            "top_logprobs": 20,
            "options": {"temperature": 0, "num_predict": 1, "num_ctx": self._s.num_ctx},
        }
        for attempt in Retrying(
            retry=retry_if_exception(_retryable),
            stop=stop_after_attempt(5),
            wait=self._retry_wait,
            reraise=True,
        ):
            with attempt:
                response = self._client.post("/api/generate", json=payload)
                response.raise_for_status()
        logprobs = response.json().get("logprobs") or []
        if not logprobs:
            raise RuntimeError(f"{self._s.model}: Ollama returned no logprobs (needs Ollama 0.12+)")
        return yes_probability(logprobs[0].get("top_logprobs") or [logprobs[0]])

    def score(self, query: str, documents: Sequence[str]) -> list[float]:
        if not documents:
            return []
        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=self._s.concurrency) as pool:
            scores = list(pool.map(lambda d: self._score_one(query, d), documents))
        self._log.debug(
            "rerank",
            model=self._s.model,
            documents=len(documents),
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
        return scores
