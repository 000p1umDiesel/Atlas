from __future__ import annotations

from collections.abc import Sequence

from mnogobase.config import GraphSettings
from mnogobase.embedding.base import Embedder
from mnogobase.models import ContextItem
from mnogobase.stores.graph_store import GraphStore
from mnogobase.stores.qdrant_store import QdrantStore


class GraphRetriever:
    """Graph-RAG: связывает запрос с сущностями, расширяет на k шагов, возвращает сущности +
    триплеты + подтверждения."""

    def __init__(
        self, graph: GraphStore, vectors: QdrantStore, embedder: Embedder, settings: GraphSettings
    ):
        self._graph = graph
        self._vectors = vectors
        self._embedder = embedder
        self._s = settings

    def retrieve(self, query: str, k: int, alt_queries: Sequence[str] = ()) -> list[ContextItem]:
        queries = [query, *alt_queries]
        vector_hits = [
            h
            for q in queries
            for h in self._vectors.search_entities(
                self._embedder.embed_query(q),
                k=self._s.seeds,
                score_threshold=self._s.seed_threshold,
            )
        ]
        vector_seeds = [h.key for h in sorted(vector_hits, key=lambda h: -h.score)]
        text_seeds = [
            eid for q in queries for eid, _ in self._graph.fulltext_entities(q, self._s.seeds)
        ]
        seeds = list(dict.fromkeys(vector_seeds + text_seeds))[: self._s.seeds * 2]
        if not seeds:
            return []
        items: list[ContextItem] = []
        for eid in seeds[: self._s.seeds]:
            entity = self._graph.get_entity(eid)
            if entity is not None:
                items.append(
                    ContextItem(
                        kind="entity",
                        ref=eid,
                        text=f"{entity.name} ({entity.type}): {entity.description}",
                    )
                )
        relations = self._graph.neighborhood(seeds, self._s.hops, self._s.max_relations)
        for r in relations:
            text = f"{r.src_name} —{r.predicate}→ {r.dst_name}"
            if r.description:
                text += f": {r.description}"
            items.append(
                ContextItem(
                    kind="relation",
                    ref=f"{r.src_id}|{r.predicate}|{r.dst_id}",
                    text=text,
                    score=r.weight,
                )
            )
        evidence = list(dict.fromkeys(cid for r in relations for cid in r.evidence))[:k]
        if evidence:
            for chunk in self._graph.chunks_by_ids(evidence):
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
