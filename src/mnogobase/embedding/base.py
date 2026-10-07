from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Protocol

from qdrant_client import models as qm

from mnogobase.models import EmbedInput


class Embedder(Protocol):
    model_id: str
    dim: int

    def embed_documents(self, items: Sequence[EmbedInput]) -> list[list[float]]: ...

    def embed_query(self, query: str) -> list[float]: ...


class SparseEncoder(Protocol):
    def encode_documents(self, texts: Sequence[str]) -> list[qm.SparseVector]: ...

    def encode_query(self, text: str) -> qm.SparseVector: ...


def format_document(item: EmbedInput, template: str) -> str:
    if item.modality != "text" or item.text is None:
        raise NotImplementedError(f"modality {item.modality!r} is not supported yet")
    return template.format(title=item.title or "none", text=item.text)


def format_query(query: str, template: str) -> str:
    return template.format(query=query)


def truncate_normalize(vec: Sequence[float], dim: int) -> list[float]:
    """Matryoshka truncation: keep the first `dim` values and re-normalize."""
    if len(vec) < dim:
        raise ValueError(f"model returned {len(vec)} dims, config expects {dim}")
    head = list(vec[:dim])
    norm = math.sqrt(sum(x * x for x in head)) or 1.0
    return [x / norm for x in head]


def embedder_signature(embedder: Embedder) -> str:
    return f"{embedder.model_id}:{embedder.dim}"
