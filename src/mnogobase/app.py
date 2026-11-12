from __future__ import annotations

import inspect
from dataclasses import dataclass, field

from mnogobase.chunking.hybrid import Chunker
from mnogobase.config import Settings
from mnogobase.database import (
    database_target,
    mark_project,
)
from mnogobase.device import detect_device
from mnogobase.embedding.base import Embedder, SparseEncoder
from mnogobase.embedding.openai_compat import OpenAICompatEmbedder
from mnogobase.embedding.sparse import BM25Encoder
from mnogobase.extraction.extractor import Extractor
from mnogobase.extraction.resolver import EntityResolver
from mnogobase.llm.client import LLMClient, OpenAICompatLLM
from mnogobase.parsing.docling_parser import DoclingParser
from mnogobase.pipeline import Pipeline
from mnogobase.registry import Registry
from mnogobase.retrieval.rerank import ApiReranker, Reranker
from mnogobase.stores.graph_store import GraphStore
from mnogobase.stores.qdrant_store import QdrantStore
from mnogobase.wiki.builder import WikiBuilder


@dataclass
class App:
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
    reranker: Reranker | None = None  # rerank.enabled
    _owned_clients: tuple[object, ...] = field(default=(), repr=False)

    def close(self) -> None:
        try:
            self.chunker.close()
        finally:
            self.graph.close()
            self.registry.close()

    async def aclose(self) -> None:
        """Закрывает ресурсы операции, сохраняя внедрённые клиентские зависимости."""
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
            from mnogobase.service.observability import UsageStore

            usage = UsageStore(database_target(settings))

            def recorder(event):
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
