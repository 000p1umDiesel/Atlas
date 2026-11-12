"""Сборка приложения: единая точка, где создаются и связываются все компоненты ядра.

`build_app(settings, ...)` вызывают команды CLI, worker очереди, `KnowledgeBase` API
(`service/kb.py`) и тесты. Она создаёт registry PostgreSQL, клиенты моделей (эмбеддер,
BM25, LLM, реранкер), хранилища (Qdrant, Neo4j), парсер Docling, чанкер, извлечение и
слияние сущностей, сборщик wiki и пайплайн ingest и возвращает их в одном `App`.

Любой из внешних клиентов (`embedder`, `sparse`, `llm`, `reranker`, `vectors`, `graph`)
можно передать готовым — так тесты подставляют фейки. `App` закрывает только то, что
создал сам (`_owned_clients`), переданные извне клиенты остаются за вызывающим.
Клиенты моделей, токенизатор и драйвер Neo4j подключаются лениво, при первом запросе:
сама сборка серверы моделей не опрашивает.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field

from mnogobase.chunking.hybrid import Chunker
from mnogobase.core.config import Settings
from mnogobase.core.database import (
    database_target,
    mark_project,
)
from mnogobase.core.device import detect_device
from mnogobase.embedding.base import Embedder, SparseEncoder
from mnogobase.embedding.openai_compat import OpenAICompatEmbedder
from mnogobase.embedding.sparse import BM25Encoder
from mnogobase.extraction.extractor import Extractor
from mnogobase.extraction.resolver import EntityResolver
from mnogobase.ingest.pipeline import Pipeline
from mnogobase.llm.client import LLMClient, OpenAICompatLLM
from mnogobase.parsing.docling_parser import DoclingParser
from mnogobase.retrieval.rerank import ApiReranker, Reranker
from mnogobase.stores.graph_store import GraphStore
from mnogobase.stores.qdrant_store import QdrantStore
from mnogobase.stores.registry import Registry
from mnogobase.wiki.builder import WikiBuilder


@dataclass
class App:
    """Собранное приложение: настройки и все компоненты ядра, готовые к работе.

    Создаётся только через `build_app`. После использования — `await app.aclose()`
    (или синхронный `close()`, если async-клиенты закрывать не нужно)."""

    settings: Settings
    registry: Registry
    embedder: Embedder
    sparse: SparseEncoder
    vectors: QdrantStore
    graph: GraphStore
    llm: LLMClient
    parser: DoclingParser
    chunker: Chunker
    wiki: WikiBuilder
    pipeline: Pipeline
    reranker: Reranker | None = None  # есть только при rerank.enabled (или передан извне)
    _owned_clients: tuple[object, ...] = field(default=(), repr=False)

    def close(self) -> None:
        """Закрывает чанкер, драйвер Neo4j и registry; клиентов моделей и Qdrant
        не трогает — это делает `aclose`.

        В отличие от остальных внедрённых зависимостей, граф закрывается всегда, даже
        если был передан извне."""
        try:
            self.chunker.close()
        finally:
            self.graph.close()
            self.registry.close()

    async def aclose(self) -> None:
        """Закрывает ресурсы операции, сохраняя внедрённые клиентские зависимости.

        Сначала `close()`, затем клиенты из `_owned_clients` (созданные самой `build_app`):
        у каждого вызывается `aclose` или `close`, корутина дожидается."""
        try:
            self.close()
        finally:
            for resource in self._owned_clients:
                close = getattr(resource, "aclose", None) or getattr(resource, "close", None)
                if close is not None:
                    result = close()
                    if inspect.isawaitable(result):
                        await result


def build_app(
    settings: Settings,
    *,
    embedder: Embedder | None = None,
    sparse: SparseEncoder | None = None,
    llm: LLMClient | None = None,
    reranker: Reranker | None = None,
    vectors: QdrantStore | None = None,
    graph: GraphStore | None = None,
) -> App:
    """Создаёт и связывает компоненты по `settings`; переданные аргументы используются
    вместо создаваемых по умолчанию.

    Побочные эффекты: открывает соединение с PostgreSQL (registry), пишет маркер проекта
    `<data_dir>/postgresql-project.json` (`mark_project`), создаёт драйвер Neo4j. LLM,
    созданная здесь, пишет учёт вызовов в `llm_usage` (`UsageStore`) со стоимостью по
    `service.llm_rates`. При ошибке посреди сборки уже открытые соединения с базами
    закрываются.

    Raises:
        DatabaseError: PostgreSQL не настроен или недоступен.
    """
    owned_clients: list[object] = []
    registry = Registry(database_target(settings))
    owned_graph = False
    try:
        mark_project(settings)
        owned_embedder, owned_vectors, owned_llm = embedder is None, vectors is None, llm is None
        embedder = embedder or OpenAICompatEmbedder(settings.embedder)
        sparse = sparse or BM25Encoder(settings.sparse.model)
        # при `embedder.dim: auto` размерность спросит у сервера только команда, которой она нужна
        vectors = vectors or QdrantStore.from_settings(settings.qdrant, lambda: embedder.dim)
        owned_graph = graph is None
        graph = graph or GraphStore.from_settings(settings.neo4j)
        if llm is None:
            # импорт внутри: пакет service нужен только когда LLM создаётся здесь
            from mnogobase.service.observability import UsageStore

            usage = UsageStore(database_target(settings))

            def recorder(event):
                """Пишет событие вызова LLM в `llm_usage`; цена — по имени модели."""
                usage.record(event, settings.service.llm_rates.get(event.model))

            llm = OpenAICompatLLM(settings.llm, usage_recorder=recorder)
        for own, resource in (
            (owned_embedder, embedder),
            (owned_vectors, vectors),
            (owned_llm, llm),
        ):
            if own:
                owned_clients.append(resource)
        if reranker is None and settings.rerank.enabled:
            reranker = ApiReranker(settings.rerank)
            owned_clients.append(reranker)
        parser = DoclingParser(
            settings.parsing,
            detect_device(settings.device),
            settings.data_dir / "cache",
            settings.vision,
        )
        chunker = Chunker(settings.chunking, settings.embedder)
        extractor = Extractor(llm, registry, settings.extract.entity_types)
        resolver = EntityResolver(graph, vectors, embedder, llm, settings.resolve)
        wiki = WikiBuilder(
            settings.wiki,
            graph,
            vectors,
            embedder,
            sparse,
            llm,
            registry,
            entity_types=list(settings.extract.entity_types),
        )
        pipeline = Pipeline(
            settings,
            registry,
            parser,
            chunker,
            embedder,
            sparse,
            vectors,
            graph,
            extractor,
            resolver,
            wiki,
        )
        return App(
            settings,
            registry,
            embedder,
            sparse,
            vectors,
            graph,
            llm,
            parser,
            chunker,
            wiki,
            pipeline,
            reranker,
            tuple(owned_clients),
        )
    except BaseException:
        # Частичная сборка не должна оставлять открытые соединения с базами.
        registry.close()
        if owned_graph and graph is not None:
            graph.close()
        raise
