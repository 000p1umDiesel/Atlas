from __future__ import annotations

import json
import math
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from neo4j import (
    Driver,
    GraphDatabase,
    ManagedTransaction,
    NotificationDisabledClassification,
    Query,
)
from neo4j.exceptions import ClientError, Neo4jError

from mnogobase.config import Neo4jSettings
from mnogobase.log import get_logger
from mnogobase.models import (
    ChunkRecord,
    ChunkView,
    DocumentRecord,
    EntityContext,
    EntityRecord,
    PageIndexRow,
    RelationView,
    WikiPageRecord,
)

# Конфиг драйвера для всех драйверов GraphStore. Уведомления UNRECOGNIZED ("relationship
# type X does not exist") нормальны для свежей базы, иначе они логировались бы как WARNING.
DRIVER_OPTIONS: dict[str, object] = {
    "notifications_disabled_classifications": [NotificationDisabledClassification.UNRECOGNIZED],
}

_SCHEMA = [
    "CREATE CONSTRAINT document_id IF NOT EXISTS FOR (d:Document) REQUIRE d.doc_id IS UNIQUE",
    "CREATE CONSTRAINT chunk_id IF NOT EXISTS FOR (c:Chunk) REQUIRE c.chunk_id IS UNIQUE",
    "CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (e:Entity) REQUIRE e.entity_id IS UNIQUE",
    "CREATE CONSTRAINT page_id IF NOT EXISTS FOR (p:WikiPage) REQUIRE p.page_id IS UNIQUE",
    "CREATE INDEX page_slug IF NOT EXISTS FOR (p:WikiPage) ON (p.slug)",
    "CREATE INDEX chunk_doc IF NOT EXISTS FOR (c:Chunk) ON (c.doc_id)",
    "CREATE FULLTEXT INDEX entity_names IF NOT EXISTS FOR (e:Entity) ON EACH [e.name, e.aliases_text]",
    "CREATE CONSTRAINT analysis_state_key IF NOT EXISTS FOR (s:AnalysisState) REQUIRE s.key IS UNIQUE",
    "CREATE INDEX entity_community IF NOT EXISTS FOR (e:Entity) "
    "ON (e.analytics_run_id, e.analytics_community)",
]
_WORD = re.compile(r"\w+", re.UNICODE)
_TEAM_ID = re.compile(r"[0-9a-f]{32}\Z")
_NODE_LABEL = re.compile(r":(Document|Chunk|Entity|WikiPage|AnalysisState)\b")
_NODE_MERGE = re.compile(r"(MERGE \(\w+:(?:Document|Chunk|Entity|WikiPage|AnalysisState) \{)")
_SCHEMA_NAME = re.compile(r"\A(CREATE (?:CONSTRAINT|(?:FULLTEXT )?INDEX)) (\w+)\b")
_PAGE_FIELDS = "p {.page_id, .slug, .title, .path, .content_hash, .version} AS page"
_REL_MAP = (
    "{src_id: startNode(r).entity_id, src_name: startNode(r).name, predicate: r.predicate, "
    "dst_id: endNode(r).entity_id, dst_name: endNode(r).name, description: r.description, "
    "weight: r.weight, evidence: r.evidence}"
)
_INVALIDATE_ANALYSIS = (
    "MERGE (s:AnalysisState {key: 'graph'}) SET s.revision = randomUUID(), s.run_id = null"
)


@dataclass
class DeleteResult:
    affected: list[str]
    removed_entity_ids: list[str]
    removed_names: list[str]
    removed_pages: list[tuple[str, str]]


def _scope_query(query: str, team_id: str | None) -> str:
    """Только фиксированные серверные Cypher-шаблоны; значения остаются параметрами.

    Командные узлы не получают legacy labels: иначе старые unique constraints и
    полнотекстовый индекс объединяли бы проекты. Unique ID внутри отдельного label
    уже задаёт уникальность пары (team_id, local_id), не меняя публичный local_id.
    Вызывается и для обычных запросов, и внутри транзакции каскадного удаления.
    """
    if team_id is None:
        return query
    # Защита находится здесь тоже: даже прямой вызов helper не интерполирует чужой Cypher.
    if not isinstance(team_id, str) or not _TEAM_ID.fullmatch(team_id):
        raise ValueError("team_id must contain exactly 32 lowercase hexadecimal characters")
    prefix = f"mb_team_{team_id}_"
    query = _NODE_MERGE.sub(r"\1team_id: $_team_id, ", query)
    query = _NODE_LABEL.sub(lambda match: f":{prefix}{match[1]}", query)
    query = _SCHEMA_NAME.sub(lambda match: f"{match[1]} {prefix}{match[2]}", query)
    return query.replace("'entity_names'", f"'{prefix}entity_names'")


class GraphStore:
    def __init__(self, driver: Driver, database: str | None = None, *, team_id: str | None = None):
        if team_id is not None and (
            not isinstance(team_id, str) or not _TEAM_ID.fullmatch(team_id)
        ):
            raise ValueError("team_id must contain exactly 32 lowercase hexadecimal characters")
        self._driver = driver
        self._db = database
        self._team_id = team_id

    @classmethod
    def from_settings(cls, settings: Neo4jSettings) -> GraphStore:
        driver = GraphDatabase.driver(
            settings.uri, auth=(settings.user, settings.password()), **DRIVER_OPTIONS
        )
        return cls(driver, team_id=settings.team_id)

    def close(self) -> None:
        self._driver.close()

    def verify(self) -> None:
        self._driver.verify_connectivity()

    def _run(self, query: str, *, _timeout: float | None = None, **params) -> list[dict]:
        query = _scope_query(query, self._team_id)
        if self._team_id is not None:
            params["_team_id"] = self._team_id
        statement = Query(query, timeout=_timeout) if _timeout is not None else query
        records, _, _ = self._driver.execute_query(
            statement, parameters_=params, database_=self._db
        )
        return [r.data() for r in records]

    def _write(self, query: str, **params) -> None:
        """Invalidate derived scores atomically with a source mutation; lock state first."""
        if self._team_id is not None:
            params["_team_id"] = self._team_id

        def write(tx):
            tx.run(_scope_query(_INVALIDATE_ANALYSIS, self._team_id), **params).consume()
            tx.run(_scope_query(query, self._team_id), **params).consume()

        with self._driver.session(database=self._db) as session:
            session.execute_write(write)

    # ---- схема / обслуживание ----
    def ensure_schema(self) -> None:
        for statement in _SCHEMA:
            self._run(statement)
        self._run("MERGE (s:AnalysisState {key: 'graph'}) ON CREATE SET s.revision = randomUUID()")
        self._run("CALL db.awaitIndexes(60)")

    def wipe(self) -> None:
        self._run(
            "MATCH (n) WHERE n:Document OR n:Chunk OR n:Entity OR n:WikiPage "
            "OR n:AnalysisState DETACH DELETE n"
        )

    def counts(self) -> dict[str, int]:
        out = {
            label: self._run(f"MATCH (n:{label}) RETURN count(n) AS n")[0]["n"]
            for label in ("Document", "Chunk", "Entity", "WikiPage")
        }
        out["RELATED"] = self._run("MATCH (:Entity)-[r:RELATED]->(:Entity) RETURN count(r) AS n")[
            0
        ]["n"]
        return out

    # ---- документы / чанки ----
    def upsert_document(self, doc: DocumentRecord) -> None:
        self._write(
            "MERGE (d:Document {doc_id: $doc_id}) "
            "SET d.path = $path, d.title = $title, d.mime = $mime, d.n_pages = $n_pages, "
            "d.ingested_at = datetime()",
            **doc.model_dump(),
        )

    def upsert_chunks(self, chunks: list[ChunkRecord]) -> None:
        rows = [c.model_dump(exclude={"context_text", "path"}) for c in chunks]
        self._write(
            "UNWIND $rows AS row "
            "MATCH (d:Document {doc_id: row.doc_id}) "
            "MERGE (c:Chunk {chunk_id: row.chunk_id}) "
            "SET c += row {.doc_id, .idx, .text, .headings, .page_start, .page_end, .n_tokens, .modality} "
            "MERGE (d)-[:HAS_CHUNK]->(c)",
            rows=rows,
        )
        pairs = [[a.chunk_id, b.chunk_id] for a, b in pairwise(chunks)]
        if pairs:
            self._write(
                "UNWIND $pairs AS p "
                "MATCH (a:Chunk {chunk_id: p[0]}), (b:Chunk {chunk_id: p[1]}) "
                "MERGE (a)-[:NEXT]->(b)",
                pairs=pairs,
            )

    def document_path(self, doc_id: str) -> str | None:
        """Путь, записанный в узле Document (None, если документ неизвестен)."""
        rows = self._run("MATCH (d:Document {doc_id: $id}) RETURN d.path AS path", id=doc_id)
        return rows[0]["path"] if rows else None

    def set_document_path(self, doc_id: str, path: str) -> None:
        self._write("MATCH (d:Document {doc_id: $id}) SET d.path = $path", id=doc_id, path=path)

    def pages_citing_document(self, doc_id: str) -> list[str]:
        """Wiki-страницы (page_id == entity_id), чьи Sources цитируют чанк этого документа."""
        rows = self._run(
            "MATCH (p:WikiPage)-[:CITES]->(:Chunk {doc_id: $id}) RETURN DISTINCT p.page_id AS pid",
            id=doc_id,
        )
        return sorted(r["pid"] for r in rows)

    def has_document(self, doc_id: str) -> bool:
        return bool(self._run("MATCH (d:Document {doc_id: $id}) RETURN 1 AS x LIMIT 1", id=doc_id))

    def document_deletion_plan(self, doc_id: str) -> DeleteResult:
        """Только чтение: что удалил бы `delete_document(doc_id)` (пусто, если документ
        неизвестен). Сущности удаляются, если их не упоминает ни один чанк вне этого
        документа."""
        rows = self._run(
            "MATCH (:Document {doc_id: $doc_id})-[:HAS_CHUNK]->(:Chunk)-[:MENTIONS]->(e:Entity) "
            "WITH DISTINCT e "
            "OPTIONAL MATCH (p:WikiPage)-[:ABOUT]->(e) "
            "WITH e, collect(p) AS pages "
            "RETURN e.entity_id AS eid, e.name AS name, "
            "[x IN pages | [x.page_id, x.path]] AS page_refs, "
            "COUNT { (e)<-[:MENTIONS]-(x:Chunk) "
            "WHERE NOT (:Document {doc_id: $doc_id})-[:HAS_CHUNK]->(x) } AS others",
            doc_id=doc_id,
        )
        removed = [r for r in rows if r["others"] == 0]
        return DeleteResult(
            affected=[r["eid"] for r in rows],
            removed_entity_ids=[r["eid"] for r in removed],
            removed_names=[r["name"] for r in removed],
            removed_pages=[(p[0], p[1]) for r in removed for p in r["page_refs"]],
        )

    def delete_document(self, doc_id: str) -> DeleteResult:
        # одна write-транзакция: падение посреди каскада не должно оставить Document удалённым,
        # а его сущности — с устаревшими счётчиками упоминаний / осиротевшими wiki-страницами
        # (повтор ничего бы не сделал)
        with self._driver.session(database=self._db) as session:
            return session.execute_write(_delete_document_tx, doc_id, self._team_id)

    def chunks_by_ids(self, chunk_ids: list[str]) -> list[ChunkView]:
        rows = self._run(
            "UNWIND $ids AS cid "
            "MATCH (d:Document)-[:HAS_CHUNK]->(c:Chunk {chunk_id: cid}) "
            "RETURN c.chunk_id AS chunk_id, c.doc_id AS doc_id, c.text AS text, d.path AS path, "
            "c.page_start AS page, coalesce(c.headings, []) AS headings",
            ids=chunk_ids,
        )
        return [ChunkView(**r) for r in rows]

    def chunks_mentioning(self, entity_id: str, chunk_ids: list[str]) -> list[ChunkView]:
        """Те из `chunk_ids`, что ещё существуют и всё ещё упоминают сущность, в порядке входа."""
        rows = self._run(
            "UNWIND range(0, size($ids) - 1) AS i "
            "MATCH (d:Document)-[:HAS_CHUNK]->(c:Chunk {chunk_id: $ids[i]})"
            "-[:MENTIONS]->(:Entity {entity_id: $eid}) "
            "RETURN c.chunk_id AS chunk_id, c.doc_id AS doc_id, c.text AS text, d.path AS path, "
            "c.page_start AS page, coalesce(c.headings, []) AS headings ORDER BY i",
            ids=chunk_ids,
            eid=entity_id,
        )
        return [ChunkView(**r) for r in rows]

    def doc_chunks(self, doc_id: str) -> tuple[DocumentRecord, list[ChunkRecord]] | None:
        """Документ и его чанки в том виде, как они хранятся в графе (None, если документ
        неизвестен).

        `context_text` не хранится; он собирается заново как заголовки + текст, по одному на
        строку, как в `contextualize` чанкера (прочие метаданные чанка, которые тот может
        добавить, например подписи, теряются)."""
        rows = self._run(
            "MATCH (d:Document {doc_id: $doc_id}) "
            "OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:Chunk) "
            "WITH d, c ORDER BY c.idx "
            "RETURN d {.doc_id, .path, .title, .mime, .n_pages} AS doc, collect(c {.*}) AS chunks",
            doc_id=doc_id,
        )
        if not rows:
            return None
        doc = DocumentRecord(**rows[0]["doc"])
        chunks = [
            ChunkRecord(
                **c,
                context_text="\n".join([*(c.get("headings") or []), c["text"]]),
                path=doc.path,
            )
            for c in rows[0]["chunks"]
        ]
        return doc, chunks

    def chunk_entity_ids(self, doc_id: str) -> dict[str, list[str]]:
        rows = self._run(
            "MATCH (c:Chunk {doc_id: $doc_id}) "
            "OPTIONAL MATCH (c)-[:MENTIONS]->(e:Entity) "
            "RETURN c.chunk_id AS cid, collect(e.entity_id) AS eids",
            doc_id=doc_id,
        )
        return {r["cid"]: r["eids"] for r in rows}

    # ---- сущности / связи ----
    def get_entity(self, entity_id: str) -> EntityRecord | None:
        rows = self._run("MATCH (e:Entity {entity_id: $id}) RETURN e {.*} AS e", id=entity_id)
        return EntityRecord(**rows[0]["e"]) if rows else None

    def upsert_entity(self, entity: EntityRecord) -> None:
        self._write(
            "MERGE (e:Entity {entity_id: $entity_id}) "
            "SET e.name = $name, e.type = $type, e.aliases = $aliases, e.aliases_text = $aliases_text, "
            "e.description = $description, e.descriptions = $descriptions, "
            "e.mention_count = coalesce(e.mention_count, 0)",
            entity_id=entity.entity_id,
            name=entity.name,
            type=entity.type,
            aliases=entity.aliases,
            aliases_text=" | ".join(entity.aliases),
            description=entity.description,
            descriptions=entity.descriptions,
        )

    def entities(self, min_mentions: int = 0) -> list[EntityRecord]:
        rows = self._run(
            "MATCH (e:Entity) WHERE e.mention_count >= $m RETURN e {.*} AS e ORDER BY e.name",
            m=min_mentions,
        )
        return [EntityRecord(**r["e"]) for r in rows]

    def add_mentions(self, chunk_id: str, entity_ids: list[str]) -> None:
        if not entity_ids:
            return
        self._write(
            "MATCH (c:Chunk {chunk_id: $chunk_id}) "
            "UNWIND $eids AS eid "
            "MATCH (e:Entity {entity_id: eid}) "
            "MERGE (c)-[:MENTIONS]->(e) "
            "WITH DISTINCT e "
            "SET e.mention_count = COUNT { (e)<-[:MENTIONS]-(:Chunk) }",
            chunk_id=chunk_id,
            eids=entity_ids,
        )

    def merge_relation(
        self,
        src_id: str,
        dst_id: str,
        predicate: str,
        description: str,
        strength: int,
        chunk_id: str,
    ) -> None:
        self._write(
            "MATCH (a:Entity {entity_id: $src}), (b:Entity {entity_id: $dst}) "
            "MERGE (a)-[r:RELATED {predicate: $predicate}]->(b) "
            "ON CREATE SET r.evidence = [], r.strengths = [], r.weight = 0.0, r.description = $description "
            "WITH r WHERE NOT $chunk_id IN r.evidence "
            "SET r.evidence = r.evidence + $chunk_id, r.strengths = r.strengths + $strength, "
            "r.weight = r.weight + $strength, "
            "r.description = CASE WHEN size($description) > size(coalesce(r.description, '')) "
            "THEN $description ELSE r.description END",
            src=src_id,
            dst=dst_id,
            predicate=predicate,
            description=description,
            strength=strength,
            chunk_id=chunk_id,
        )

    def entity_context(self, entity_id: str, max_relations: int) -> EntityContext:
        rows = self._run(
            "MATCH (e:Entity {entity_id: $id}) "
            "OPTIONAL MATCH (e)-[r:RELATED]-(:Entity) "
            "WITH e, r ORDER BY r.weight DESC "
            "WITH e, collect(r)[..$limit] AS rels "
            f"RETURN e {{.*}} AS entity, [r IN rels | {_REL_MAP}] AS relations",
            id=entity_id,
            limit=max_relations,
        )
        if not rows:
            raise KeyError(entity_id)
        return EntityContext(
            entity=EntityRecord(**rows[0]["entity"]),
            relations=[RelationView(**r) for r in rows[0]["relations"]],
        )

    def fulltext_entities(self, query: str, k: int) -> list[tuple[str, float]]:
        # простые словесные токены в нижнем регистре: операторы Lucene и спецсимволы не проходят
        tokens = [t.casefold() for t in _WORD.findall(query)]
        if not tokens:
            return []
        rows = self._run(
            "CALL db.index.fulltext.queryNodes('entity_names', $q) YIELD node, score "
            "RETURN node.entity_id AS id, score LIMIT $k",
            q=" ".join(tokens),
            k=k,
        )
        return [(r["id"], r["score"]) for r in rows]

    def neighborhood(self, seed_ids: list[str], hops: int, limit: int) -> list[RelationView]:
        hops = max(1, min(int(hops), 3))  # границы переменной длины не могут быть параметрами
        rows = self._run(
            "MATCH (s:Entity) WHERE s.entity_id IN $seeds "
            f"MATCH p = (s)-[:RELATED*1..{hops}]-(:Entity) "
            "WHERE all(n IN nodes(p) WHERE n:Entity) "
            "WITH p LIMIT 5000 "
            "UNWIND relationships(p) AS r "
            "WITH DISTINCT r "
            "WITH r, startNode(r) AS a, endNode(r) AS b "
            "WITH r, a, b, "
            "COUNT { (a)--(n) WHERE n:Document OR n:Chunk OR n:Entity OR n:WikiPage } + "
            "COUNT { (b)--(n) WHERE n:Document OR n:Chunk OR n:Entity OR n:WikiPage } AS degree "
            "RETURN a.entity_id AS src_id, a.name AS src_name, r.predicate AS predicate, "
            "b.entity_id AS dst_id, b.name AS dst_name, r.description AS description, "
            "r.weight AS weight, r.evidence AS evidence, degree "
            "ORDER BY weight DESC, degree DESC LIMIT $limit",
            seeds=seed_ids,
            limit=limit,
        )
        return [RelationView(**r) for r in rows]

    def neighborhood_apoc(
        self,
        seed_ids: list[str],
        hops: int,
        limit: int,
        *,
        max_nodes: int = 20_000,
        timeout: float = 5.0,
    ) -> list[RelationView]:
        if not seed_ids or limit <= 0:
            return []
        count = self._run("MATCH (e:Entity) RETURN count(e) AS n")[0]["n"]
        if count > max_nodes:
            get_logger(__name__).info("apoc_fallback", reason="node_budget", nodes=count)
            return self.neighborhood(seed_ids, hops, limit)
        try:
            rows = self._run(
                "MATCH (n:Entity) WITH collect(n) AS allowed "
                "UNWIND $seeds AS sid MATCH (s:Entity {entity_id: sid}) "
                "CALL apoc.path.expandConfig(s, {relationshipFilter:'RELATED', "
                "labelFilter:$label_filter, allowlistNodes:allowed, filterStartNode:true, "
                "minLevel:0, maxLevel:$hops, bfs:true, uniqueness:'NODE_GLOBAL', limit:5000}) "
                "YIELD path WITH path LIMIT 5000 "
                "WITH collect(DISTINCT last(nodes(path))) AS reached "
                "UNWIND reached AS a MATCH (a)-[r:RELATED]-(b:Entity) WHERE b IN reached "
                "WITH DISTINCT r WITH r, startNode(r) AS a, endNode(r) AS b "
                "RETURN a.entity_id AS src_id, a.name AS src_name, r.predicate AS predicate, "
                "b.entity_id AS dst_id, b.name AS dst_name, r.description AS description, "
                "r.weight AS weight, r.evidence AS evidence "
                "ORDER BY weight DESC, src_id, predicate, dst_id LIMIT $limit",
                seeds=seed_ids,
                hops=max(1, min(int(hops), 3)),
                limit=limit,
                label_filter=f"+{self.entity_label}",
                _timeout=timeout,
            )
        except ClientError as exc:
            if exc.code != "Neo.ClientError.Procedure.ProcedureNotFound":
                raise
            get_logger(__name__).warning("apoc_fallback", reason="procedure_missing")
            return self.neighborhood(seed_ids, hops, limit)
        return [RelationView(**r) for r in rows]

    # ---- производная аналитика (не меняет IDs и evidence) ----
    @property
    def entity_label(self) -> str:
        return f"mb_team_{self._team_id}_Entity" if self._team_id else "Entity"

    @property
    def analysis_scope(self) -> str:
        return self._team_id or "legacy"

    def analysis_status(self) -> dict:
        rows = self._run(
            "MATCH (s:AnalysisState {key: 'graph'}) "
            "RETURN s.revision AS revision, s.run_id AS run_id, s.metadata AS metadata, "
            "toString(s.completed_at) AS completed_at"
        )
        state = rows[0] if rows else {"revision": None, "run_id": None}
        metadata = json.loads(state.pop("metadata", None) or "{}")
        return {**state, "ready": bool(state["run_id"]), "metadata": metadata}

    def analysis_scores(self, entity_ids: list[str]) -> dict[str, float]:
        rows = self._run(
            "MATCH (s:AnalysisState {key: 'graph'}) WHERE s.run_id IS NOT NULL "
            "MATCH (e:Entity) WHERE e.entity_id IN $ids AND e.analytics_run_id = s.run_id "
            "RETURN e.entity_id AS id, e.analytics_pagerank AS score",
            ids=entity_ids,
        )
        return {r["id"]: r["score"] for r in rows}

    def analysis_details(self, limit: int = 20) -> list[dict]:
        return self._run(
            "MATCH (s:AnalysisState {key: 'graph'}) WHERE s.run_id IS NOT NULL "
            "MATCH (e:Entity) WHERE e.analytics_run_id = s.run_id "
            "RETURN e.entity_id AS entity_id, e.name AS name, "
            "e.analytics_pagerank AS pagerank, e.analytics_community AS community "
            "ORDER BY pagerank DESC, entity_id LIMIT $limit",
            limit=max(1, min(limit, 100)),
        )

    def community_chunks(
        self,
        seed_ids: list[str],
        limit: int,
        *,
        max_communities: int = 3,
        max_entities: int = 30,
        exclude_chunk_ids: Sequence[str] = (),
        timeout: float = 5.0,
    ) -> list[ChunkView]:
        """Original evidence from current seed communities, bounded before MENTIONS expansion."""
        if not seed_ids or limit <= 0 or max_communities <= 0 or max_entities <= 0:
            return []
        try:
            rows = self._run(
                "MATCH (s:AnalysisState {key:'graph'}) WHERE s.run_id IS NOT NULL "
                "UNWIND range(0, size($seeds)-1) AS priority "
                "MATCH (seed:Entity {entity_id:$seeds[priority]}) "
                "WHERE seed.analytics_run_id = s.run_id "
                "AND seed.analytics_community IS NOT NULL "
                "WITH s, seed.analytics_community AS community, min(priority) AS priority "
                "ORDER BY priority, community LIMIT $groups "
                "CALL (s, community) { "
                "  MATCH (e:Entity) WHERE e.analytics_run_id = s.run_id "
                "  AND e.analytics_community = community "
                "  RETURN e ORDER BY e.analytics_pagerank DESC, e.entity_id LIMIT $members "
                "} "
                "MATCH (d:Document)-[:HAS_CHUNK]->(c:Chunk)-[:MENTIONS]->(e) "
                "WHERE NOT c.chunk_id IN $excluded "
                "WITH d, c, min(priority) AS priority, count(DISTINCT e) AS mentions, "
                "max(e.analytics_pagerank) AS rank "
                "RETURN c.chunk_id AS chunk_id, c.doc_id AS doc_id, c.text AS text, "
                "d.path AS path, c.page_start AS page, coalesce(c.headings,[]) AS headings "
                "ORDER BY priority, mentions DESC, rank DESC, chunk_id LIMIT $limit",
                seeds=list(dict.fromkeys(seed_ids)),
                groups=min(max_communities, 20),
                members=min(max_entities, 500),
                excluded=list(exclude_chunk_ids),
                limit=min(limit, 100),
                _timeout=timeout,
            )
        except Neo4jError as exc:
            if "TransactionTimedOut" not in (exc.code or ""):
                raise
            get_logger(__name__).warning("leiden_context_timeout")
            return []
        return [ChunkView(**row) for row in rows]

    def plugin_status(self) -> dict:
        procedures = {
            "apoc.path.expandConfig",
            "gds.graph.project",
            "gds.graph.project.estimate",
            "gds.graph.list",
            "gds.graph.drop",
            "gds.pageRank.stream",
            "gds.pageRank.stream.estimate",
            "gds.leiden.stream",
            "gds.leiden.stream.estimate",
        }
        available = {
            r["name"]
            for r in self._run(
                "SHOW PROCEDURES YIELD name WHERE name IN $names RETURN name",
                names=sorted(procedures),
            )
        }
        functions = {
            r["name"]
            for r in self._run(
                "SHOW FUNCTIONS YIELD name WHERE name IN $names RETURN name",
                names=["apoc.version", "gds.version", "gds.util.asNode"],
            )
        }
        return {
            "apoc": self._run("RETURN apoc.version() AS version")[0]["version"]
            if "apoc.version" in functions
            else None,
            "gds": self._run("RETURN gds.version() AS version")[0]["version"]
            if "gds.version" in functions
            else None,
            "missing": sorted((procedures - available) | ({"gds.util.asNode"} - functions)),
        }

    def analysis_counts(self) -> dict:
        nodes = self._run("MATCH (e:Entity) RETURN count(e) AS n")[0]["n"]
        relationships = self._run(
            "MATCH (:Entity)-[r:RELATED]->(:Entity) "
            "RETURN count(r) AS relationships, sum(CASE WHEN r.weight > 0 "
            "AND r.weight <= 1.7976931348623157e308 THEN 0 ELSE 1 END) AS invalid_weights"
        )[0]
        return {"nodes": nodes, **relationships}

    def gds_project_estimate(self, timeout: float) -> int:
        return int(
            self._run(
                "CALL gds.graph.project.estimate($label, "
                "{RELATED:{orientation:'UNDIRECTED', properties:'weight'}}, "
                "{readConcurrency:1, validateRelationships:false}) "
                "YIELD bytesMax RETURN bytesMax AS bytes",
                label=self.entity_label,
                _timeout=timeout,
            )[0]["bytes"]
        )

    def gds_project(self, name: str, timeout: float) -> None:
        self._run(
            "CALL gds.graph.project($name, $label, "
            "{RELATED:{orientation:'UNDIRECTED', properties:'weight'}}, "
            "{readConcurrency:1, validateRelationships:false}) YIELD graphName RETURN graphName",
            name=name,
            label=self.entity_label,
            _timeout=timeout,
        )

    def gds_algorithm_estimate(self, name: str, algorithm: str, timeout: float) -> int:
        if algorithm not in {"pageRank", "leiden"}:
            raise ValueError("Unsupported graph algorithm")
        return int(
            self._run(
                f"CALL gds.{algorithm}.stream.estimate($name, "
                "{relationshipWeightProperty:'weight', concurrency:1}) "
                "YIELD bytesMax RETURN bytesMax AS bytes",
                name=name,
                _timeout=timeout,
            )[0]["bytes"]
        )

    def gds_pagerank(self, name: str, timeout: float) -> list[dict]:
        return self._run(
            "CALL gds.pageRank.stream($name, {relationshipWeightProperty:'weight', "
            "concurrency:1, maxIterations:20, dampingFactor:0.85}) "
            "YIELD nodeId, score RETURN gds.util.asNode(nodeId).entity_id AS entity_id, "
            "score AS pagerank",
            name=name,
            _timeout=timeout,
        )

    def gds_leiden(self, name: str, timeout: float) -> list[dict]:
        return self._run(
            "CALL gds.leiden.stream($name, {relationshipWeightProperty:'weight', "
            "concurrency:1, randomSeed:42}) YIELD nodeId, communityId "
            "RETURN gds.util.asNode(nodeId).entity_id AS entity_id, communityId AS community_id",
            name=name,
            _timeout=timeout,
        )

    def owned_projections(self) -> list[str]:
        return [
            r["name"]
            for r in self._run(
                "CALL gds.graph.list() YIELD graphName "
                "WHERE graphName STARTS WITH $prefix RETURN graphName AS name",
                prefix=f"mb_analysis_{self.analysis_scope}_",
            )
        ]

    def drop_projection(self, name: str) -> None:
        if not name.startswith(f"mb_analysis_{self.analysis_scope}_"):
            raise ValueError("Cannot drop another scope's projection")
        self._run("CALL gds.graph.drop($name, false) YIELD graphName RETURN graphName", name=name)

    def clear_analysis(self) -> None:
        self._run(
            "MATCH (s:AnalysisState {key: 'graph'}) SET s.run_id = null, s.metadata = null, "
            "s.completed_at = null"
        )
        self._run(
            "MATCH (e:Entity) REMOVE e.analytics_run_id, e.analytics_pagerank, e.analytics_community"
        )

    def publish_analysis(self, revision: str, rows: list[dict], metadata: dict) -> bool:
        if len({r["entity_id"] for r in rows}) != len(rows) or any(
            not math.isfinite(r["pagerank"]) or r["pagerank"] < 0 or not r["community"]
            for r in rows
        ):
            raise ValueError("Analysis requires complete, unique, finite scores")
        run_id = uuid.uuid4().hex

        def publish(tx):
            def run(query, **params):
                if self._team_id:
                    params["_team_id"] = self._team_id
                return tx.run(_scope_query(query, self._team_id), **params)

            # Taking a write lock before reading the revision serializes with source writes.
            state = run(
                "MATCH (s:AnalysisState {key: 'graph'}) "
                "SET s.publications = coalesce(s.publications, 0) + 1 RETURN s.revision AS revision"
            ).single()
            if state is None or state["revision"] != revision:
                return False
            count = run("MATCH (e:Entity) RETURN count(e) AS n").single()["n"]
            if count != len(rows):
                raise ValueError("Analysis must contain the complete entity set")
            written = run(
                "UNWIND $rows AS row MATCH (e:Entity {entity_id: row.entity_id}) "
                "SET e.analytics_run_id = $run, e.analytics_pagerank = row.pagerank, "
                "e.analytics_community = row.community RETURN count(e) AS n",
                rows=rows,
                run=run_id,
            ).single()["n"]
            if written != count:
                raise ValueError("Analysis must contain the complete entity set")
            run(
                "MATCH (s:AnalysisState {key: 'graph'}) SET s.run_id = $run, "
                "s.metadata = $metadata, s.completed_at = datetime()",
                run=run_id,
                metadata=json.dumps(metadata, ensure_ascii=False),
            ).consume()
            return True

        with self._driver.session(database=self._db) as session:
            return session.execute_write(publish)

    # ---- wiki-страницы ----
    def upsert_wiki_page(
        self, page: WikiPageRecord, entity_id: str, links_to: list[str], cites: list[str]
    ) -> None:
        self._run(
            "MERGE (p:WikiPage {page_id: $page_id}) "
            "SET p.slug = $slug, p.title = $title, p.path = $path, p.content_hash = $content_hash, "
            "p.version = $version, p.updated_at = datetime() "
            "WITH p MATCH (e:Entity {entity_id: $entity_id}) MERGE (p)-[:ABOUT]->(e)",
            entity_id=entity_id,
            **page.model_dump(),
        )
        self._run(
            "MATCH (p:WikiPage {page_id: $page_id})-[r:LINKS_TO|CITES]->() DELETE r",
            page_id=page.page_id,
        )
        self._run(
            "MATCH (p:WikiPage {page_id: $page_id}) "
            "UNWIND $links AS target MATCH (q:WikiPage {page_id: target}) MERGE (p)-[:LINKS_TO]->(q)",
            page_id=page.page_id,
            links=links_to,
        )
        self._run(
            "MATCH (p:WikiPage {page_id: $page_id}) "
            "UNWIND $cites AS cid MATCH (c:Chunk {chunk_id: cid}) MERGE (p)-[:CITES]->(c)",
            page_id=page.page_id,
            cites=cites,
        )

    def delete_wiki_page(self, page_id: str) -> None:
        """Удаляет узел страницы и его рёбра; сущность, которой посвящена страница, остаётся."""
        self._run("MATCH (p:WikiPage {page_id: $id}) DETACH DELETE p", id=page_id)

    def pages_linking_to(self, page_ids: list[str]) -> list[str]:
        """Id страниц с LINKS_TO на любую из `page_ids`, не считая самих `page_ids`."""
        rows = self._run(
            "MATCH (p:WikiPage)-[:LINKS_TO]->(q:WikiPage) "
            "WHERE q.page_id IN $ids AND NOT p.page_id IN $ids "
            "RETURN DISTINCT p.page_id AS id ORDER BY id",
            ids=page_ids,
        )
        return [r["id"] for r in rows]

    def wiki_page(self, page_id: str) -> WikiPageRecord | None:
        rows = self._run(f"MATCH (p:WikiPage {{page_id: $id}}) RETURN {_PAGE_FIELDS}", id=page_id)
        return WikiPageRecord(**rows[0]["page"]) if rows else None

    def page_by_slug(self, slug: str) -> WikiPageRecord | None:
        rows = self._run(f"MATCH (p:WikiPage {{slug: $slug}}) RETURN {_PAGE_FIELDS}", slug=slug)
        return WikiPageRecord(**rows[0]["page"]) if rows else None

    def wiki_pages(self) -> list[PageIndexRow]:
        rows = self._run(
            "MATCH (p:WikiPage)-[:ABOUT]->(e:Entity) "
            "RETURN p.page_id AS page_id, p.slug AS slug, p.title AS title, p.path AS path, "
            "e.type AS entity_type, coalesce(e.aliases, []) AS aliases "
            "ORDER BY entity_type, title"
        )
        return [PageIndexRow(**r) for r in rows]


def _delete_document_tx(
    tx: ManagedTransaction, doc_id: str, team_id: str | None = None
) -> DeleteResult:
    def run(query: str, **params):
        if team_id:
            params["_team_id"] = team_id
        return tx.run(_scope_query(query, team_id), **params)

    run(_INVALIDATE_ANALYSIS).consume()
    found = run(
        "MATCH (d:Document {doc_id: $doc_id}) "
        "OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:Chunk) "
        "OPTIONAL MATCH (c)-[:MENTIONS]->(e:Entity) "
        "RETURN collect(DISTINCT c.chunk_id) AS cids, collect(DISTINCT e.entity_id) AS eids",
        doc_id=doc_id,
    ).data()
    cids = found[0]["cids"] if found else []
    eids = found[0]["eids"] if found else []
    if cids:
        # убираем подтверждения из удалённых чанков; рёбра, оставшиеся без подтверждений, исчезают
        run(
            "MATCH (:Entity)-[r:RELATED]->(:Entity) "
            "WHERE any(x IN r.evidence WHERE x IN $cids) "
            "WITH r, [i IN range(0, size(r.evidence) - 1) WHERE NOT r.evidence[i] IN $cids] AS keep "
            "SET r.description = '', r.evidence = [i IN keep | r.evidence[i]], "
            "    r.strengths = [i IN keep | r.strengths[i]] "
            "SET r.weight = reduce(s = 0.0, x IN r.strengths | s + x) "
            "WITH r WHERE size(r.evidence) = 0 DELETE r",
            cids=cids,
        ).consume()
    run(
        "MATCH (d:Document {doc_id: $doc_id}) "
        "OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:Chunk) "
        "WITH d, collect(c) AS cs "
        "FOREACH (x IN cs | DETACH DELETE x) "
        "DETACH DELETE d",
        doc_id=doc_id,
    ).consume()
    removed = run(
        "UNWIND $eids AS eid "
        "MATCH (e:Entity {entity_id: eid}) "
        "SET e.mention_count = COUNT { (e)<-[:MENTIONS]-(:Chunk) } "
        "WITH e WHERE e.mention_count = 0 "
        "OPTIONAL MATCH (p:WikiPage)-[:ABOUT]->(e) "
        "WITH e, e.entity_id AS eid, e.name AS name, collect(p) AS pages "
        "WITH e, eid, name, pages, [x IN pages | [x.page_id, x.path]] AS page_refs "
        "FOREACH (x IN pages | DETACH DELETE x) "
        "DETACH DELETE e "
        "RETURN eid, name, page_refs",
        eids=eids,
    ).data()
    return DeleteResult(
        affected=eids,
        removed_entity_ids=[r["eid"] for r in removed],
        removed_names=[r["name"] for r in removed],
        removed_pages=[(p[0], p[1]) for r in removed for p in r["page_refs"]],
    )
