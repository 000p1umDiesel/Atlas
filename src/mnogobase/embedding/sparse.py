from __future__ import annotations

from collections.abc import Sequence

from qdrant_client import models as qm


def _to_qdrant(embedding) -> qm.SparseVector:
    return qm.SparseVector(indices=embedding.indices.tolist(), values=embedding.values.tolist())


class BM25Encoder:
    """BM25 term weights via fastembed; IDF is applied by Qdrant (Modifier.IDF)."""

    def __init__(self, model: str = "Qdrant/bm25", device: str = "cpu"):
        self._model_name = model
        self._cuda = device == "cuda"
        self._model = None

    def _get(self):
        if self._model is None:
            from fastembed import SparseTextEmbedding

            kwargs = {"cuda": True} if self._cuda else {}  # needs onnxruntime-gpu
            self._model = SparseTextEmbedding(self._model_name, **kwargs)
        return self._model

    def encode_documents(self, texts: Sequence[str]) -> list[qm.SparseVector]:
        return [_to_qdrant(e) for e in self._get().passage_embed(list(texts))]

    def encode_query(self, text: str) -> qm.SparseVector:
        return _to_qdrant(next(iter(self._get().query_embed(text))))
