from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from typing import Protocol

from qdrant_client import models as qm

from mnogobase.models import EmbedInput


class Embedder(Protocol):
    model_id: str
    dim: int
    templates: tuple[str, str]  # (document, query) templates the embedder applies

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


def _legacy_signature(embedder: Embedder) -> str:
    """The signature before the templates were part of it."""
    return f"{embedder.model_id}:{embedder.dim}"


def embedder_signature(embedder: Embedder) -> str:
    """What stored vectors depend on: `model_id:dim:tpl-<hash of both templates>`."""
    templates = json.dumps(list(embedder.templates), ensure_ascii=False)
    digest = hashlib.sha256(templates.encode("utf-8")).hexdigest()[:8]
    return f"{_legacy_signature(embedder)}:tpl-{digest}"


def signature_mismatch(stored: str | None, embedder: Embedder) -> str | None:
    """Why an index signed `stored` cannot be used with `embedder`; None if it can.

    No signature (no index yet) is fine, and so is a legacy `model_id:dim` one of the same
    model and dimension: such an index was built with the templates of its time, and the
    caller rewrites the signature in the current format."""
    current = embedder_signature(embedder)
    if stored in (None, current, _legacy_signature(embedder)):
        return None
    reason = f"index was built with {stored}, config now uses {current}"
    if stored.startswith(f"{_legacy_signature(embedder)}:tpl-"):
        reason += " (embedder templates changed: doc_template / query_template)"
    return reason
