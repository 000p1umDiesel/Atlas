from __future__ import annotations

from mnogobase.embedding.base import Embedder, SparseEncoder
from mnogobase.models import ContextItem
from mnogobase.stores.graph_store import GraphStore
from mnogobase.stores.qdrant_store import QdrantStore


class WikiRetriever:
    """Persistent-wiki mode: hybrid search over wiki page sections.

    Each hit's cited chunks (payload `chunk_ids`) follow the sections as `chunk` items, so a
    wiki answer can cite the underlying file, page and chunk id.
    """

    def __init__(
        self, vectors: QdrantStore, embedder: Embedder, sparse: SparseEncoder, graph: GraphStore
    ):
        self._vectors = vectors
        self._embedder = embedder
        self._sparse = sparse
        self._graph = graph

    def retrieve(self, query: str, k: int) -> list[ContextItem]:
        hits = self._vectors.search_wiki(
            self._embedder.embed_query(query), self._sparse.encode_query(query), k
        )
        items = [
            ContextItem(
                kind="wiki",
                ref=h.key,
                text=h.payload["text"],
                path=h.payload.get("path"),
                score=h.score,
            )
            for h in hits
        ]
        cited = list(dict.fromkeys(c for h in hits for c in h.payload.get("chunk_ids") or []))[:k]
        if cited:
            for chunk in self._graph.chunks_by_ids(cited):
                items.append(
                    ContextItem(
                        kind="chunk",
                        ref=chunk.chunk_id,
                        text=chunk.text,
                        path=chunk.path,
                        page=chunk.page,
                    )
                )
        return items
