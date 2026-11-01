from __future__ import annotations

import time
from collections.abc import Sequence

import httpx
from tenacity import Retrying, retry_if_exception, stop_after_attempt

from mnogobase.config import EmbedderSettings
from mnogobase.embedding.base import format_document, format_query, truncate_normalize
from mnogobase.embedding.http import DEFAULT_RETRY_WAIT, auth_headers, retryable
from mnogobase.log import get_logger
from mnogobase.models import EmbedInput


class OpenAICompatEmbedder:
    """`/embeddings` OpenAI-совместимого сервера: llama.cpp server, vLLM, LM Studio, TEI.

    Контекст задаётся при запуске сервера, а длинный вход сервер не обрезает, а отклоняет:
    чанки не длиннее `chunking.max_tokens` должны помещаться в его окно. Повтор при сетевых
    ошибках и 5xx. При `dim: auto` размерность — длина первого вектора от сервера."""

    def __init__(
        self,
        settings: EmbedderSettings,
        client: httpx.Client | None = None,
        retry_wait=DEFAULT_RETRY_WAIT,
    ):
        self._s = settings
        self._client = client or httpx.Client(
            base_url=settings.url, timeout=120, headers=auth_headers(settings.api_key_env)
        )
        self._retry_wait = retry_wait
        self.model_id = settings.model_id
        self._dim = settings.dim if settings.dim != "auto" else None
        self.templates = (settings.doc_template, settings.query_template)
        self._log = get_logger(__name__)

    @property
    def dim(self) -> int:
        """Размерность векторов; при `dim: auto` до первого вектора её узнаёт пробный запрос."""
        if self._dim is None:
            self._embed(["dimension probe"])
        assert self._dim is not None
        return self._dim

    def _embed(self, inputs: list[str]) -> list[list[float]]:
        start = time.perf_counter()
        for attempt in Retrying(
            retry=retry_if_exception(retryable),
            stop=stop_after_attempt(5),
            wait=self._retry_wait,
            reraise=True,
        ):
            with attempt:
                response = self._client.post(
                    "/embeddings", json={"model": self._s.model, "input": inputs}
                )
                response.raise_for_status()
        data = sorted(response.json()["data"], key=lambda d: d["index"])
        if [item["index"] for item in data] != list(range(len(inputs))):
            raise ValueError("embedding API returned missing/duplicate indexes")
        self._log.debug(
            "embed_batch",
            model=self._s.model,
            batch_size=len(inputs),
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
        dim = self._dim if self._dim is not None else len(data[0]["embedding"])
        vectors = [truncate_normalize(item["embedding"], dim) for item in data]
        self._dim = dim
        return vectors

    def embed_documents(self, items: Sequence[EmbedInput]) -> list[list[float]]:
        texts = [format_document(item, self._s.doc_template) for item in items]
        out: list[list[float]] = []
        for start in range(0, len(texts), self._s.batch_size):
            out.extend(self._embed(texts[start : start + self._s.batch_size]))
        return out

    def embed_query(self, query: str) -> list[float]:
        return self._embed([format_query(query, self._s.query_template)])[0]
