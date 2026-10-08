from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from mnogobase.embedding.base import Embedder, SparseEncoder
from mnogobase.models import ContextItem
from mnogobase.stores.qdrant_store import QueryVectors


class Retriever(Protocol):
    """`alt_queries` are other phrasings of the same question searched alongside it (e.g. its
    English translation, so BM25 and English entity names still match English sources)."""

    def retrieve(
        self, query: str, k: int, alt_queries: Sequence[str] = ()
    ) -> list[ContextItem]: ...


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def query_vectors(
    embedder: Embedder, sparse: SparseEncoder, queries: Sequence[str]
) -> list[QueryVectors]:
    return [(embedder.embed_query(q), sparse.encode_query(q)) for q in queries]
