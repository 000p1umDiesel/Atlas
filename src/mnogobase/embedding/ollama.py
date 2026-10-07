from __future__ import annotations

import time
from collections.abc import Sequence

import httpx
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

from mnogobase.config import EmbedderSettings
from mnogobase.embedding.base import format_document, format_query, truncate_normalize
from mnogobase.log import get_logger
from mnogobase.models import EmbedInput

_DEFAULT_RETRY_WAIT = wait_exponential(multiplier=1, max=20)


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500


class OllamaEmbedder:
    def __init__(
        self,
        settings: EmbedderSettings,
        client: httpx.Client | None = None,
        retry_wait=_DEFAULT_RETRY_WAIT,
    ):
        self._s = settings
        self._client = client or httpx.Client(base_url=settings.base_url, timeout=120)
        self._retry_wait = retry_wait
        self.model_id = f"ollama:{settings.model}"
        self.dim = settings.dim
        self.templates = (settings.doc_template, settings.query_template)
        self._log = get_logger(__name__)

    def _embed(self, inputs: list[str]) -> list[list[float]]:
        start = time.perf_counter()
        for attempt in Retrying(
            retry=retry_if_exception(_retryable),
            stop=stop_after_attempt(5),
            wait=self._retry_wait,
            reraise=True,
        ):
            with attempt:
                response = self._client.post(
                    "/api/embed", json={"model": self._s.model, "input": inputs, "truncate": True}
                )
                response.raise_for_status()
        vectors = response.json()["embeddings"]
        self._log.debug(
            "embed_batch",
            model=self._s.model,
            batch_size=len(inputs),
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
        return [truncate_normalize(v, self.dim) for v in vectors]

    def embed_documents(self, items: Sequence[EmbedInput]) -> list[list[float]]:
        texts = [format_document(item, self._s.doc_template) for item in items]
        out: list[list[float]] = []
        for start in range(0, len(texts), self._s.batch_size):
            out.extend(self._embed(texts[start : start + self._s.batch_size]))
        return out

    def embed_query(self, query: str) -> list[float]:
        return self._embed([format_query(query, self._s.query_template)])[0]
