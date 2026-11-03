from __future__ import annotations

from collections.abc import Sequence

from mnogobase.config import GraphSettings
from mnogobase.embedding.base import Embedder
from mnogobase.log import get_logger
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
        if k <= 0:
            return []
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
        if self._s.traversal == "apoc":
            relations = self._graph.neighborhood_apoc(
                seeds,
                self._s.hops,
                self._s.max_relations,
                max_nodes=self._s.analysis.apoc_max_nodes,
                timeout=self._s.analysis.apoc_timeout_s,
            )
        else:
            relations = self._graph.neighborhood(seeds, self._s.hops, self._s.max_relations)
        if self._s.use_gds and relations:
            entity_ids = list(dict.fromkeys(eid for r in relations for eid in (r.src_id, r.dst_id)))
            scores = self._graph.analysis_scores(entity_ids)
            if len(scores) == len(entity_ids):
                # Stable ties retain the traversal's previous order. Scores never add candidates.
                relations.sort(key=lambda r: (-r.weight, -(scores[r.src_id] + scores[r.dst_id])))
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
        evidence = list(dict.fromkeys(cid for r in relations for cid in r.evidence))
        local = self._graph.chunks_by_ids(evidence[:k]) if evidence else []
        by_id = {c.chunk_id: c for c in local}
        local = [by_id[cid] for cid in evidence[:k] if cid in by_id]
        community = self._s.community
        extra = []
        quota = min(community.max_chunks, k // 2 if local else k)
        if self._s.use_gds and community.enabled and quota:
            extra = self._graph.community_chunks(
                seeds,
                quota,
                max_communities=community.max_communities,
                max_entities=community.max_entities,
                exclude_chunk_ids=evidence,
                timeout=community.timeout_s,
            )
            get_logger(__name__).info("leiden_context", chunks=len(extra), seeds=len(seeds))
        chunks = [
            ContextItem(kind="chunk", ref=c.chunk_id, text=c.text, path=c.path, page=c.page)
            for c in [*local[: k - len(extra)], *extra]
        ]
        # Give original evidence priority over summaries within the answer/context budget.
        return chunks + items if extra else items + chunks
