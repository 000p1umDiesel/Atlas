"""Мелочи для работы с Qdrant: id точек, sparse-векторы, проверка схемы коллекции."""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from qdrant_client import models as qm


def point_id(chunk_id: str) -> str:
    """Qdrant принимает только UUID или целые числа, поэтому выводим UUID из chunk_id.

    Один и тот же чанк всегда получает один и тот же id, и повторная запись
    просто перезаписывает точку.
    """
    return str(uuid5(NAMESPACE_URL, chunk_id))


def sparse_vector(embedding) -> qm.SparseVector:
    return qm.SparseVector(indices=embedding.indices.tolist(), values=embedding.values.tolist())


def chunk_text(payload: dict) -> str:
    """Текст чанка вместе с цепочкой заголовков: так его видят reranker и LLM."""
    headings = payload.get("headings") or []
    return (" > ".join(headings) + "\n" if headings else "") + payload["text"]


def check_collection(client, name: str, dimension: int) -> None:
    params = client.get_collection(name).config.params
    dense = params.vectors.get("dense") if isinstance(params.vectors, dict) else None
    sparse = (params.sparse_vectors or {}).get("bm25")
    if (
        dense is None
        or dense.size != dimension
        or dense.distance != qm.Distance.COSINE
        or sparse is None
        or sparse.modifier != qm.Modifier.IDF
    ):
        raise ValueError(f"Коллекция {name} устроена иначе; задайте другой INDEX_NAME")
