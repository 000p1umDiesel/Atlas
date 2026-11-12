"""Dense-эмбеддер через `/embeddings` OpenAI-совместимого сервера.

По умолчанию это llama.cpp server с Qwen3-Embedding GGUF на порту 8001 (секция
`embedder`). Используется при ingest (чанки, wiki-секции, сущности) и при каждом поиске
(вектор запроса). Клиент синхронный (httpx), повторы — `post_json_with_retry`.
"""

from __future__ import annotations

import time
from collections.abc import Sequence

import httpx

from mnogobase.core.config import EmbedderSettings
from mnogobase.core.http import DEFAULT_RETRY_WAIT, auth_headers, post_json_with_retry
from mnogobase.core.log import get_logger
from mnogobase.core.models import EmbedInput
from mnogobase.embedding.base import format_document, format_query, truncate_normalize


class OpenAICompatEmbedder:
    """`/embeddings` OpenAI-совместимого сервера: llama.cpp server, LM Studio, TEI.

    Контекст задаётся при запуске сервера, а длинный вход сервер не обрезает, а отклоняет:
    чанки не длиннее `chunking.max_tokens` должны помещаться в его окно. Повтор при сетевых
    ошибках и 5xx. При `dim: auto` размерность — длина первого вектора от сервера."""

    def __init__(
        self,
        settings: EmbedderSettings,
        client: httpx.Client | None = None,
        retry_wait=DEFAULT_RETRY_WAIT,
    ):
        """`client` — готовый httpx-клиент (тесты); иначе создаётся свой. Сеть при создании
        не трогается: при `dim: auto` размерность узнаётся лениво."""
        self._s = settings
        self._client = client or httpx.Client(
            base_url=settings.url,
            timeout=settings.timeout_s,
            headers=auth_headers(settings.api_key_env),
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

    def close(self) -> None:
        """Закрывает HTTP-клиент."""
        self._client.close()

    def _embed(self, inputs: list[str]) -> list[list[float]]:
        """Один запрос `/embeddings` на весь список; векторы в порядке `inputs`.

        Ответ сортируется по `index` и проверяется, что пришёл ровно один вектор на каждый
        вход (иначе `ValueError`). Векторы усекаются до `dim` и нормируются; при `dim: auto`
        первый ответ фиксирует размерность. Ошибки HTTP после повторов пробрасываются.
        """
        start = time.perf_counter()
        response = post_json_with_retry(
            self._client,
            "/embeddings",
            {"model": self._s.model, "input": inputs},
            wait=self._retry_wait,
        )
        data = sorted(response.json()["data"], key=lambda d: d["index"])
        if [item["index"] for item in data] != list(range(len(inputs))):
            raise ValueError("API эмбеддингов вернул пропущенные или повторяющиеся индексы")
        self._log.debug(
            "embed_batch",
            model=self._s.model,
            batch_size=len(inputs),
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
        dim = self._dim if self._dim is not None else len(data[0]["embedding"])
        vectors = [truncate_normalize(item["embedding"], dim) for item in data]
        # размерность фиксируется только после того, как все векторы прошли проверку
        self._dim = dim
        return vectors

    def embed_documents(self, items: Sequence[EmbedInput]) -> list[list[float]]:
        """Векторы документов пачками по `embedder.batch_size` (шаблон `doc_template`)."""
        texts = [format_document(item, self._s.doc_template) for item in items]
        out: list[list[float]] = []
        for start in range(0, len(texts), self._s.batch_size):
            out.extend(self._embed(texts[start : start + self._s.batch_size]))
        return out

    def embed_query(self, query: str) -> list[float]:
        """Вектор поискового запроса (шаблон `query_template`)."""
        return self._embed([format_query(query, self._s.query_template)])[0]
