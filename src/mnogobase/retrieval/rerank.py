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

# Qwen3-Reranker's own format: the score is P("yes") for the next token after the empty think.
_PREFIX = (
    "<|im_start|>system\nJudge whether the Document meets the requirements based on the Query "
    'and the Instruct provided. Note that the answer can only be "yes" or "no".<|im_end|>\n'
    "<|im_start|>user\n"
)
_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"


class Reranker(Protocol):
    def score(self, query: str, documents: Sequence[str]) -> list[float]:
        """Relevance of each document to `query` in [0, 1], in input order."""
        ...


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500


def yes_probability(top_logprobs: list[dict]) -> float:
    """P(yes) / (P(yes) + P(no)) over the candidate first tokens (any case, any spacing)."""
    p = {"yes": 0.0, "no": 0.0}
    for t in top_logprobs:
        word = t.get("token", "").strip().casefold()
        if word in p:
            p[word] += math.exp(t["logprob"])
    total = p["yes"] + p["no"]
    return p["yes"] / total if total else 0.0


class OllamaReranker:
    """Qwen3-Reranker via Ollama `/api/generate` (raw prompt, one token, its logprobs).

    Ollama has no rerank endpoint, so each document is one generate request; `concurrency`
    of them run in parallel (Ollama serves up to `OLLAMA_NUM_PARALLEL` at once).
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
