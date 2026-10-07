from __future__ import annotations

from mnogobase.config import GraphSettings
from mnogobase.embedding.base import Embedder
from mnogobase.models import ContextItem
from mnogobase.stores.graph_store import GraphStore
from mnogobase.stores.qdrant_store import QdrantStore


class GraphRetriever:
    """Graph-RAG: link query to entities, expand k hops, return entities + triples + evidence."""

    def __init__(
        self, graph: GraphStore, vectors: QdrantStore, embedder: Embedder, settings: GraphSettings
    ):
        self._graph = graph
        self._vectors = vectors
        self._embedder = embedder
        self._s = settings

    def retrieve(self, query: str, k: int) -> list[ContextItem]:
        vector_seeds = [
            h.key
            for h in self._vectors.search_entities(
                self._embedder.embed_query(query),
                k=self._s.seeds,
                score_threshold=self._s.seed_threshold,
            )
        ]
        text_seeds = [eid for eid, _ in self._graph.fulltext_entities(query, self._s.seeds)]
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
