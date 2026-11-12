"""Режим `wiki`: поиск по секциям сгенерированных wiki-страниц.

Страницы строит `wiki.builder.WikiBuilder`; при записи каждая страница режется на секции
(`wiki.render.split_sections`), и в Qdrant попадает текст секции без маркеров цитат, а id
процитированных чанков — в payload `chunk_ids`. Здесь гибридный поиск находит секции, а
их исходные чанки подтягиваются из Neo4j, чтобы ответ ссылался на документы. У устаревших
(stale) страниц векторы удаляются ещё при инвалидации, поэтому здесь они не находятся.
"""

from __future__ import annotations

from collections.abc import Sequence

from mnogobase.core.models import ContextItem
from mnogobase.embedding.base import Embedder, SparseEncoder
from mnogobase.retrieval.base import query_vectors
from mnogobase.stores.graph_store import GraphStore
from mnogobase.stores.qdrant_store import QdrantStore


class WikiRetriever:
    """Режим persistent-wiki: гибридный поиск по секциям wiki-страниц.

    За каждой найденной секцией идут её цитируемые чанки (payload `chunk_ids`, всего не
    больше k различных) как элементы `chunk`, так что ответ по wiki может сослаться на
    исходный файл, страницу и chunk id.
    """

    def __init__(
        self, vectors: QdrantStore, embedder: Embedder, sparse: SparseEncoder, graph: GraphStore
    ):
        self._vectors = vectors
        self._embedder = embedder
        self._sparse = sparse
        self._graph = graph

    def retrieve(self, query: str, k: int, alt_queries: Sequence[str] = ()) -> list[ContextItem]:
        """До `k` секций (`wiki`) вперемешку с их чанками (`chunk`), по убыванию релевантности.

        Чанк, уже выданный после более сильной секции, повторно не выдаётся. Чанки, которых
        больше нет в графе (документ удалён), молча пропускаются.
        """
        hits = self._vectors.search_wiki(
            self._embedder.embed_query(query),
            self._sparse.encode_query(query),
            k,
            extra=query_vectors(self._embedder, self._sparse, alt_queries),
        )
        cited = list(dict.fromkeys(c for h in hits for c in h.payload.get("chunk_ids") or []))[:k]
        chunks = {c.chunk_id: c for c in self._graph.chunks_by_ids(cited)} if cited else {}
        # за каждой секцией идут её собственные цитируемые чанки, так что бюджет токенов,
        # обрезающий хвост списка, всё равно сохраняет подтверждения для оставшихся секций
        items: list[ContextItem] = []
        emitted: set[str] = set()
        for h in hits:
            items.append(
                ContextItem(
                    kind="wiki",
                    ref=h.key,
                    text=h.payload["text"],
                    path=h.payload.get("path"),
                    score=h.score,
                )
            )
            for cid in h.payload.get("chunk_ids") or []:
                chunk = chunks.get(cid)
                if chunk is None or cid in emitted:
                    continue
                emitted.add(cid)
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
