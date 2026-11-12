"""Режим `graph` (Graph-RAG): ответ по графу знаний Neo4j.

Шаги `GraphRetriever.retrieve`:

1. Семена. Сущности, похожие на вопрос: векторный поиск по коллекции сущностей Qdrant (порог
   `graph.seed_threshold`) и полнотекстовый индекс имён в Neo4j, для исходного вопроса и
   каждой `alt_queries`. Векторные семена идут первыми (по убыванию score), затем
   полнотекстовые; дубли убираются, остаётся не больше `2 * graph.seeds`.
2. Сущности. Для первых `graph.seeds` семян — элемент `entity` с типом и описанием.
3. Обход. Связи `RELATED` в пределах `graph.hops` шагов от всех семян, не больше
   `graph.max_relations`: обычным Cypher или ограниченным APOC BFS (`graph.traversal`).
   При `graph.use_gds` связи с равным весом дополнительно упорядочиваются по сумме PageRank
   концов из сохранённого снимка `graph analyze` (сам GDS здесь не запускается).
4. Подтверждения. Чанки, на которых держатся найденные связи (`evidence`), — до `k` штук в
   порядке связей; при включённых сообществах (`graph.community`) часть квоты отдаётся чанкам
   из Leiden-сообществ семян.

Результат — элементы `entity`, `relation` и `chunk`; порядок задаёт приоритет при обрезке по
бюджету токенов.
"""

from __future__ import annotations

from collections.abc import Sequence

from mnogobase.core.config import GraphSettings
from mnogobase.core.log import get_logger
from mnogobase.core.models import ContextItem
from mnogobase.embedding.base import Embedder
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
        """Ищет семена, обходит граф и собирает контекст (алгоритм — в docstring модуля).

        `k` ограничивает число чанков-подтверждений; при `k <= 0` или без семян возвращает
        пустой список. Ошибки Neo4j / Qdrant пробрасываются.
        """
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
        # для обхода берём вдвое больше семян, чем показываем сущностей: лишние семена
        # дают связи, но не тратят бюджет на собственные описания
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
            # Оценки используются, только если текущий снимок покрывает все концы связей;
            # частичный или устаревший снимок (сущности добавлены после `graph analyze`)
            # оставляет порядок обхода как есть.
            if len(scores) == len(entity_ids):
                # Сортировка стабильна: при равенстве сохраняется прежний порядок обхода.
                # PageRank только переупорядочивает связи и никогда не добавляет новых.
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
        # chunks_by_ids не гарантирует порядок и пропускает удалённые чанки:
        # восстанавливаем порядок связей
        by_id = {c.chunk_id: c for c in local}
        local = [by_id[cid] for cid in evidence[:k] if cid in by_id]
        community = self._s.community
        extra = []
        # при наличии собственных подтверждений сообществам достаётся не больше половины k
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
        # С чанками сообществ исходные тексты идут раньше описаний сущностей и связей, чтобы
        # бюджет ответа/контекста отдавал приоритет подтверждениям, а не пересказу.
        return chunks + items if extra else items + chunks
