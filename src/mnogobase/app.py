from __future__ import annotations

from dataclasses import dataclass

from mnogobase.chunking.hybrid import Chunker
from mnogobase.config import Settings
from mnogobase.device import detect_device
from mnogobase.embedding.base import Embedder, SparseEncoder
from mnogobase.embedding.ollama import OllamaEmbedder
from mnogobase.embedding.sparse import BM25Encoder
from mnogobase.extraction.extractor import Extractor
from mnogobase.extraction.resolver import EntityResolver
from mnogobase.llm.client import LLMClient, OpenAICompatLLM
from mnogobase.parsing.docling_parser import DoclingParser
from mnogobase.pipeline import Pipeline
from mnogobase.registry import Registry
from mnogobase.retrieval.rerank import OllamaReranker, Reranker
from mnogobase.stores.graph_store import GraphStore
from mnogobase.stores.qdrant_store import QdrantStore
from mnogobase.wiki.builder import WikiBuilder


@dataclass
class App:
    settings: Settings
    device: str
    registry: Registry
    embedder: Embedder
    sparse: SparseEncoder
    vectors: QdrantStore
    graph: GraphStore
    llm: LLMClient
    parser: DoclingParser
    chunker: Chunker
    extractor: Extractor
    resolver: EntityResolver
    wiki: WikiBuilder
    pipeline: Pipeline
    reranker: Reranker | None = None  # rerank.enabled

    def close(self) -> None:
        self.graph.close()
        self.registry.close()


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
    device = detect_device(settings.device)
    registry = Registry(settings.data_dir / "state.db")
    embedder = embedder or OllamaEmbedder(settings.embedder)
    sparse = sparse or BM25Encoder(settings.sparse.model, device=device)
    vectors = vectors or QdrantStore.from_settings(settings.qdrant, embedder.dim)
    graph = graph or GraphStore.from_settings(settings.neo4j)
    llm = llm or OpenAICompatLLM(settings.llm)
    if reranker is None and settings.rerank.enabled:
        reranker = OllamaReranker(settings.rerank)
    parser = DoclingParser(settings.parsing, device, settings.data_dir / "cache")
    chunker = Chunker(settings.chunking)
    extractor = Extractor(llm, registry, settings.extract.entity_types)
    resolver = EntityResolver(graph, vectors, embedder, llm, settings.resolve)
    wiki = WikiBuilder(settings.wiki, graph, vectors, embedder, sparse, llm, registry)
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
        device,
        registry,
        embedder,
        sparse,
        vectors,
        graph,
        llm,
        parser,
        chunker,
        extractor,
        resolver,
        wiki,
        pipeline,
        reranker,
    )
