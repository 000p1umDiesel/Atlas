from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Protocol

import httpx
from tenacity import Retrying, retry_if_exception, stop_after_attempt

from mnogobase.config import RerankSettings
from mnogobase.embedding.http import DEFAULT_RETRY_WAIT, auth_headers, retryable
from mnogobase.log import get_logger


class Reranker(Protocol):
    def score(self, query: str, documents: Sequence[str]) -> list[float]:
        """Релевантность каждого документа к `query` в [0, 1], в порядке входа."""
        ...


class ApiReranker:
    def close(self) -> None:
        self._client.close()

    """Cross-encoder через `/rerank` OpenAI-совместимого сервера (vLLM, llama.cpp server).

    Формат Jina/Cohere: `query` и `documents` одним запросом, ответ `results[].index` и
    `relevance_score` в [0, 1]. Промпт Qwen3-Reranker сервер строит сам (vLLM — с
    `--chat-template .../qwen3_reranker.jinja`, llama.cpp — из GGUF), поэтому тексты уходят
    как есть.
    """

    def __init__(
        self,
        settings: RerankSettings,
        client: httpx.Client | None = None,
        retry_wait=DEFAULT_RETRY_WAIT,
    ):
        self._s = settings
        self._client = client or httpx.Client(
            base_url=settings.url, timeout=120, headers=auth_headers(settings.api_key_env)
        )
        self._retry_wait = retry_wait
        self._log = get_logger(__name__)

    def score(self, query: str, documents: Sequence[str]) -> list[float]:
        if not documents:
            return []
        start = time.perf_counter()
        payload = {
            "model": self._s.model,
            "query": query,
            "documents": [d[: self._s.max_chars] for d in documents],
            "top_n": len(documents),
        }
        for attempt in Retrying(
            retry=retry_if_exception(retryable),
            stop=stop_after_attempt(5),
            wait=self._retry_wait,
            reraise=True,
        ):
            with attempt:
                response = self._client.post("/rerank", json=payload)
                response.raise_for_status()
        scores = [0.0] * len(documents)
        for result in response.json()["results"]:  # сервер сортирует по score, а не по входу
            scores[result["index"]] = float(result["relevance_score"])
        self._log.debug(
            "rerank",
            model=self._s.model,
            documents=len(documents),
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
        return scores
