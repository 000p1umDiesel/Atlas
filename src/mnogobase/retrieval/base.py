from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from mnogobase.embedding.base import Embedder, SparseEncoder
from mnogobase.models import ContextItem
from mnogobase.stores.qdrant_store import QueryVectors


class Retriever(Protocol):
    """`alt_queries` — другие формулировки того же вопроса, по которым ищем вместе с ним
    (например, его английский перевод, чтобы BM25 и английские имена сущностей находили
    английские источники)."""

    def retrieve(
        self, query: str, k: int, alt_queries: Sequence[str] = ()
    ) -> list[ContextItem]: ...


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def query_vectors(
    embedder: Embedder, sparse: SparseEncoder, queries: Sequence[str]
) -> list[QueryVectors]:
    return [(embedder.embed_query(q), sparse.encode_query(q)) for q in queries]
