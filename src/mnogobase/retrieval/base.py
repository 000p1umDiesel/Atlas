"""Общий интерфейс retriever'ов и мелкие помощники поиска.

`Retriever` — `typing.Protocol`, которому следуют все режимы (`rag`, `wiki`, `graph`, `all`);
тесты подставляют свои реализации. Здесь же грубая оценка токенов, по которой режим `all` и
`Answerer` режут контекст, и построение пар векторов (dense + sparse) для дополнительных
формулировок запроса.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from mnogobase.core.models import ContextItem
from mnogobase.embedding.base import Embedder, SparseEncoder
from mnogobase.stores.qdrant_store import QueryVectors


class Retriever(Protocol):
    """Источник контекста для ответа: по вопросу возвращает упорядоченные `ContextItem`.

    `alt_queries` — другие формулировки того же вопроса, по которым ищем вместе с ним
    (например, его английский перевод, чтобы BM25 и английские имена сущностей находили
    английские источники)."""

    def retrieve(self, query: str, k: int, alt_queries: Sequence[str] = ()) -> list[ContextItem]:
        """Ищет контекст для `query`; `k` — сколько основных попаданий брать (точный смысл
        зависит от режима). Порядок результата — приоритет: при нехватке бюджета токенов
        отбрасывается хвост."""
        ...


def estimate_tokens(text: str) -> int:
    """Грубая оценка числа токенов: ~4 символа на токен, минимум 1.

    Точный токенизатор здесь не нужен: оценка служит только для деления бюджета контекста."""
    return max(1, len(text) // 4)


def query_vectors(
    embedder: Embedder, sparse: SparseEncoder, queries: Sequence[str]
) -> list[QueryVectors]:
    """Пары (dense, sparse) для дополнительных формулировок запроса — аргумент `extra`
    гибридного поиска `QdrantStore`. Каждая формулировка стоит один вызов эмбеддера."""
    return [(embedder.embed_query(q), sparse.encode_query(q)) for q in queries]
