from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import pairwise

from neo4j import Driver, GraphDatabase, ManagedTransaction, NotificationDisabledClassification

from mnogobase.config import Neo4jSettings
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

# Driver config for every GraphStore driver. UNRECOGNIZED notifications ("relationship type X
# does not exist") are normal on a fresh database and would otherwise be logged as WARNINGs.
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
]
_WORD = re.compile(r"\w+", re.UNICODE)
_PAGE_FIELDS = "p {.page_id, .slug, .title, .path, .content_hash, .version} AS page"
_REL_MAP = (
    "{src_id: startNode(r).entity_id, src_name: startNode(r).name, predicate: r.predicate, "
    "dst_id: endNode(r).entity_id, dst_name: endNode(r).name, description: r.description, "
    "weight: r.weight, evidence: r.evidence}"
)


@dataclass
class DeleteResult:
    affected: list[str]
    removed_entity_ids: list[str]
    removed_names: list[str]
    removed_pages: list[tuple[str, str]]


class GraphStore:
    def __init__(self, driver: Driver, database: str | None = None):
        self._driver = driver
        self._db = database

    @classmethod
    def from_settings(cls, settings: Neo4jSettings) -> GraphStore:
        driver = GraphDatabase.driver(
            settings.uri, auth=(settings.user, settings.password()), **DRIVER_OPTIONS
        )
        return cls(driver)

    def close(self) -> None:
        self._driver.close()

    def verify(self) -> None:
        self._driver.verify_connectivity()

    def _run(self, query: str, **params) -> list[dict]:
        records, _, _ = self._driver.execute_query(query, parameters_=params, database_=self._db)
        return [r.data() for r in records]

    # ---- schema / maintenance ----
    def ensure_schema(self) -> None:
        for statement in _SCHEMA:
            self._run(statement)
        self._run("CALL db.awaitIndexes(60)")

    def wipe(self) -> None:
        self._run("MATCH (n) DETACH DELETE n")

    def counts(self) -> dict[str, int]:
        out = {
            label: self._run(f"MATCH (n:{label}) RETURN count(n) AS n")[0]["n"]
            for label in ("Document", "Chunk", "Entity", "WikiPage")
        }
        out["RELATED"] = self._run("MATCH ()-[r:RELATED]->() RETURN count(r) AS n")[0]["n"]
        return out

    # ---- documents / chunks ----
    def upsert_document(self, doc: DocumentRecord) -> None:
        self._run(
            "MERGE (d:Document {doc_id: $doc_id}) "
            "SET d.path = $path, d.title = $title, d.mime = $mime, d.n_pages = $n_pages, "
            "d.ingested_at = datetime()",
            **doc.model_dump(),
        )

    def upsert_chunks(self, chunks: list[ChunkRecord]) -> None:
        rows = [c.model_dump(exclude={"context_text", "path"}) for c in chunks]
        self._run(
            "UNWIND $rows AS row "
            "MATCH (d:Document {doc_id: row.doc_id}) "
            "MERGE (c:Chunk {chunk_id: row.chunk_id}) "
            "SET c += row {.doc_id, .idx, .text, .headings, .page_start, .page_end, .n_tokens, .modality} "
            "MERGE (d)-[:HAS_CHUNK]->(c)",
            rows=rows,
        )
        pairs = [[a.chunk_id, b.chunk_id] for a, b in pairwise(chunks)]
        if pairs:
            self._run(
                "UNWIND $pairs AS p "
                "MATCH (a:Chunk {chunk_id: p[0]}), (b:Chunk {chunk_id: p[1]}) "
                "MERGE (a)-[:NEXT]->(b)",
                pairs=pairs,
            )

    def document_deletion_plan(self, doc_id: str) -> DeleteResult:
        """Read-only: what `delete_document(doc_id)` would remove (empty if the document is
        unknown). Entities are removed when no chunk outside this document mentions them."""
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
        # one write transaction: a crash mid-cascade must not leave the Document gone while
        # its entities keep stale mention counts / orphaned wiki pages (a retry would be a no-op)
        with self._driver.session(database=self._db) as session:
            return session.execute_write(_delete_document_tx, doc_id)

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
        """Those of `chunk_ids` that still exist and still mention the entity, in input order."""
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
        """A document and its chunks as stored in the graph (None if the document is unknown).

        `context_text` is not stored; it is rebuilt as headings + text, one per line, like the
        chunker's `contextualize` (other chunk metadata it may add, e.g. captions, is lost)."""
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

    # ---- entities / relations ----
    def get_entity(self, entity_id: str) -> EntityRecord | None:
        rows = self._run("MATCH (e:Entity {entity_id: $id}) RETURN e {.*} AS e", id=entity_id)
        return EntityRecord(**rows[0]["e"]) if rows else None

    def upsert_entity(self, entity: EntityRecord) -> None:
        self._run(
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
        self._run(
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
        self._run(
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
        # plain lower-cased word tokens: no Lucene operators or special characters survive
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
        hops = max(1, min(int(hops), 3))  # variable-length bounds cannot be parameters
        rows = self._run(
            "MATCH (s:Entity) WHERE s.entity_id IN $seeds "
            f"MATCH p = (s)-[:RELATED*1..{hops}]-(:Entity) "
            "WITH p LIMIT 5000 "
            "UNWIND relationships(p) AS r "
            "WITH DISTINCT r "
            "WITH r, startNode(r) AS a, endNode(r) AS b "
            "WITH r, a, b, COUNT { (a)--() } + COUNT { (b)--() } AS degree "
            "RETURN a.entity_id AS src_id, a.name AS src_name, r.predicate AS predicate, "
            "b.entity_id AS dst_id, b.name AS dst_name, r.description AS description, "
            "r.weight AS weight, r.evidence AS evidence, degree "
            "ORDER BY weight DESC, degree DESC LIMIT $limit",
            seeds=seed_ids,
            limit=limit,
        )
        return [RelationView(**r) for r in rows]

    # ---- wiki pages ----
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
        """Remove the page node and its edges; the entity it is about stays."""
        self._run("MATCH (p:WikiPage {page_id: $id}) DETACH DELETE p", id=page_id)

    def pages_linking_to(self, page_ids: list[str]) -> list[str]:
        """Ids of pages with LINKS_TO into any of `page_ids`, excluding `page_ids` themselves."""
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


def _delete_document_tx(tx: ManagedTransaction, doc_id: str) -> DeleteResult:
    found = tx.run(
        "MATCH (d:Document {doc_id: $doc_id}) "
        "OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:Chunk) "
        "OPTIONAL MATCH (c)-[:MENTIONS]->(e:Entity) "
        "RETURN collect(DISTINCT c.chunk_id) AS cids, collect(DISTINCT e.entity_id) AS eids",
        doc_id=doc_id,
    ).data()
    cids = found[0]["cids"] if found else []
    eids = found[0]["eids"] if found else []
    if cids:
        # drop evidence coming from the deleted chunks; edges left without evidence disappear
        tx.run(
            "MATCH ()-[r:RELATED]->() WHERE any(x IN r.evidence WHERE x IN $cids) "
            "WITH r, [i IN range(0, size(r.evidence) - 1) WHERE NOT r.evidence[i] IN $cids] AS keep "
            "SET r.evidence = [i IN keep | r.evidence[i]], "
            "    r.strengths = [i IN keep | r.strengths[i]] "
            "SET r.weight = reduce(s = 0.0, x IN r.strengths | s + x) "
            "WITH r WHERE size(r.evidence) = 0 DELETE r",
            cids=cids,
        ).consume()
    tx.run(
        "MATCH (d:Document {doc_id: $doc_id}) "
        "OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:Chunk) "
        "WITH d, collect(c) AS cs "
        "FOREACH (x IN cs | DETACH DELETE x) "
        "DETACH DELETE d",
        doc_id=doc_id,
    ).consume()
    removed = tx.run(
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
