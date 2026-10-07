from __future__ import annotations

from mnogobase.embedding.base import Embedder, SparseEncoder
from mnogobase.models import ContextItem
from mnogobase.stores.qdrant_store import QdrantStore


class RagRetriever:
    """Classic RAG: hybrid (dense + BM25, RRF) search over raw chunks."""

    def __init__(self, vectors: QdrantStore, embedder: Embedder, sparse: SparseEncoder):
        self._vectors = vectors
        self._embedder = embedder
        self._sparse = sparse

    def retrieve(self, query: str, k: int) -> list[ContextItem]:
        hits = self._vectors.search_chunks(
            self._embedder.embed_query(query), self._sparse.encode_query(query), k
        )
        return [
            ContextItem(
                kind="chunk",
                ref=h.key,
                text=h.payload["text"],
                path=h.payload.get("path"),
                page=h.payload.get("page"),
                score=h.score,
            )
            for h in hits
        ]
