"""Разреженные BM25-векторы для гибридного поиска (секция `sparse`).

fastembed считает только частотную часть BM25 по токенам; IDF по всей коллекции
применяет сам Qdrant (sparse-вектор с `Modifier.IDF`), поэтому векторы документов не
зависят от остального корпуса и не пересчитываются при добавлении новых документов.
Модель загружается лениво при первом вызове, а не при импорте.
"""

from __future__ import annotations

from collections.abc import Sequence

from qdrant_client import models as qm


def _to_qdrant(embedding) -> qm.SparseVector:
    """fastembed `SparseEmbedding` (numpy-массивы) → `qdrant_client` `SparseVector`."""
    return qm.SparseVector(indices=embedding.indices.tolist(), values=embedding.values.tolist())


class BM25Encoder:
    """Веса термов BM25 через fastembed; IDF применяет Qdrant (Modifier.IDF)."""

    def __init__(self, model: str = "Qdrant/bm25"):
        self._model_name = model
        self._model = None

    def _get(self):
        """Модель fastembed, создаётся при первом обращении (при первом запуске может скачать
        её файлы)."""
        if self._model is None:
            from fastembed import SparseTextEmbedding

            self._model = SparseTextEmbedding(self._model_name)
        return self._model

    def encode_documents(self, texts: Sequence[str]) -> list[qm.SparseVector]:
        """Sparse-векторы документов в порядке входа."""
        return [_to_qdrant(e) for e in self._get().passage_embed(list(texts))]

    def encode_query(self, text: str) -> qm.SparseVector:
        """Sparse-вектор запроса (частотные BM25-веса считаются только у документов)."""
        return _to_qdrant(next(iter(self._get().query_embed(text))))
