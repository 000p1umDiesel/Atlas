from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from mnogobase.embedding.base import Embedder, SparseEncoder
from mnogobase.ids import chunk_id
from mnogobase.log import get_logger
from mnogobase.models import ContextItem, SearchHit
from mnogobase.retrieval.base import query_vectors
from mnogobase.retrieval.rerank import Reranker
from mnogobase.stores.qdrant_store import QdrantStore


class RagRetriever:
    """Классический RAG: гибридный (dense + BM25, RRF) поиск по исходным чанкам.

    С `reranker` лучшие `candidates` гибридных попаданий переоцениваются им, и остаются top k
    (если он падает, используется гибридный порядок и в лог пишется warning).

    Каждый элемент начинается с пути заголовков чанка. При `neighbors > 0` попадание
    расширяется максимум на столько же соседних чанков с каждой стороны, пока у них те же
    заголовки (та же секция); попадание, уже покрытое окном более сильного, отбрасывается.
    """

    def __init__(
        self,
        vectors: QdrantStore,
        embedder: Embedder,
        sparse: SparseEncoder,
        neighbors: int = 0,
        reranker: Reranker | None = None,
        candidates: int = 30,
    ):
        self._vectors = vectors
        self._embedder = embedder
        self._sparse = sparse
        self._neighbors = neighbors
        self._reranker = reranker
        self._candidates = candidates
        self._log = get_logger(__name__)

    def retrieve(self, query: str, k: int, alt_queries: Sequence[str] = ()) -> list[ContextItem]:
        hits = self._vectors.search_chunks(
            self._embedder.embed_query(query),
            self._sparse.encode_query(query),
            max(k, self._candidates) if self._reranker else k,
            extra=query_vectors(self._embedder, self._sparse, alt_queries),
        )
        if self._reranker:
            hits = self._rerank(query, hits)[:k]
        if self._neighbors <= 0:
            return [_item(h.key, [h.payload], h.score) for h in hits]

        n = self._neighbors
        wanted = []
        for h in hits:
            doc_id, idx = _split(h.key)
            wanted += [chunk_id(doc_id, i) for i in range(max(0, idx - n), idx + n + 1)]
        known = self._vectors.get_chunks(list(dict.fromkeys(wanted)))
        known.update({h.key: h.payload for h in hits})

        covered: set[str] = set()
        items: list[ContextItem] = []
        for h in hits:
            if h.key in covered:
                continue
            doc_id, idx = _split(h.key)
            section = h.payload.get("headings") or []
            block = [h.key]
            for step in (-1, 1):
                for i in range(idx + step, idx + step * (n + 1), step):
                    key = chunk_id(doc_id, i)
                    other = known.get(key) if i >= 0 else None
                    if other is None or key in covered or (other.get("headings") or []) != section:
                        break
                    block = [key, *block] if step < 0 else [*block, key]
            covered.update(block)
            items.append(_item(h.key, [known[c] for c in block], h.score))
        return items

    def _rerank(self, query: str, hits: list[SearchHit]) -> list[SearchHit]:
        texts = [_item(h.key, [h.payload], h.score).text for h in hits]
        try:
            scores = self._reranker.score(query, texts)
        except Exception as exc:  # ответ всё равно работает на гибридном порядке
            self._log.warning("rerank_failed", error_type=type(exc).__name__, error=str(exc))
            return hits
        # стабильная сортировка: при равных score сохраняется гибридный порядок
        order = sorted(range(len(hits)), key=lambda i: -scores[i])
        return [hits[i].model_copy(update={"score": scores[i]}) for i in order]


def _split(key: str) -> tuple[str, int]:
    doc_id, idx = key.rsplit(":", 1)
    return doc_id, int(idx)


def _item(ref: str, payloads: list[dict[str, Any]], score: float) -> ContextItem:
    """Один элемент контекста из подряд идущих чанков секции; цитируется как `ref` попадания."""
    first = payloads[0]
    headings = first.get("headings") or []
    texts = [p["text"] for p in payloads]
    if headings:
        texts.insert(0, " > ".join(headings))
    pages = [p["page"] for p in payloads if p.get("page") is not None]
    return ContextItem(
        kind="chunk",
        ref=ref,
        text="\n".join(texts),
        path=first.get("path"),
        page=min(pages) if pages else None,
        score=score,
    )
