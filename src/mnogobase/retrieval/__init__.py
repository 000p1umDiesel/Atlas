from __future__ import annotations

from enum import StrEnum

from mnogobase.config import Settings
from mnogobase.embedding.base import Embedder, SparseEncoder
from mnogobase.retrieval.base import Retriever
from mnogobase.retrieval.combined import CombinedRetriever
from mnogobase.retrieval.graph import GraphRetriever
from mnogobase.retrieval.rag import RagRetriever
from mnogobase.retrieval.wiki import WikiRetriever
from mnogobase.stores.graph_store import GraphStore
from mnogobase.stores.qdrant_store import QdrantStore


class Mode(StrEnum):
    RAG = "rag"
    WIKI = "wiki"
    GRAPH = "graph"
    ALL = "all"


def build_retrievers(
    vectors: QdrantStore,
    graph: GraphStore,
    embedder: Embedder,
    sparse: SparseEncoder,
    settings: Settings,
) -> dict[str, Retriever]:
    """One retriever per `Mode`, keyed by the mode value (`rag`, `wiki`, `graph`, `all`)."""
    rag = RagRetriever(vectors, embedder, sparse)
    wiki = WikiRetriever(vectors, embedder, sparse, graph)
    graph_retriever = GraphRetriever(graph, vectors, embedder, settings.graph)
    combined = CombinedRetriever(
        {"rag": rag, "wiki": wiki, "graph": graph_retriever},
        settings.retrieval.budget,
        settings.retrieval.context_tokens,
    )
    return {
        Mode.RAG.value: rag,
        Mode.WIKI.value: wiki,
        Mode.GRAPH.value: graph_retriever,
        Mode.ALL.value: combined,
    }
