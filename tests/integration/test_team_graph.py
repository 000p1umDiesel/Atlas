import pytest

from mnogobase.models import ChunkRecord, DocumentRecord, EntityRecord, WikiPageRecord
from mnogobase.stores.graph_store import DRIVER_OPTIONS, GraphStore

pytestmark = pytest.mark.integration
DOC_ID = "d" * 16
CHUNKS = [f"{DOC_ID}:{i:05d}" for i in range(2)]


@pytest.fixture
def scopes(neo4j_container):
    stores = [
        GraphStore(neo4j_container.get_driver(**DRIVER_OPTIONS), team_id=team_id)
        for team_id in (None, "a" * 32, "b" * 32)
    ]
    for store in stores:
        store.wipe()
        store.ensure_schema()
    try:
        yield stores
    finally:
        for store in stores:
            store.wipe()
            store.close()


def populate(store, title):
    store.upsert_document(
        DocumentRecord(doc_id=DOC_ID, path=f"/{title}.md", title=title, mime="text/markdown")
    )
    store.upsert_chunks(
        [
            ChunkRecord(
                chunk_id=cid,
                doc_id=DOC_ID,
                idx=i,
                text=f"{title} body {i}",
                context_text=f"{title} body {i}",
            )
            for i, cid in enumerate(CHUNKS)
        ]
    )
    for eid in ("e1", "e2"):
        store.upsert_entity(EntityRecord(entity_id=eid, name=f"{title} {eid}", type="Concept"))
        store.add_mentions(CHUNKS[0], [eid])
        store.upsert_wiki_page(
            WikiPageRecord(page_id=eid, slug=eid, title=title, path=f"{title}/{eid}.md"),
            eid,
            [],
            [CHUNKS[0]],
        )
    store.upsert_wiki_page(store.wiki_page("e1"), "e1", ["e2"], [CHUNKS[1]])
    store.merge_relation("e1", "e2", "uses", title, 3, CHUNKS[0])
    store.merge_relation("e1", "e2", "uses", title, 2, CHUNKS[1])


def test_same_ids_documents_chunks_entities_and_pages_are_local(scopes):
    for store, title in zip(scopes, ("Legacy", "Alpha", "Beta"), strict=True):
        populate(store, title)
    for store, title in zip(scopes, ("Legacy", "Alpha", "Beta"), strict=True):
        assert store.counts() == {
            "Document": 1,
            "Chunk": 2,
            "Entity": 2,
            "WikiPage": 2,
            "RELATED": 1,
        }
        assert store.document_path(DOC_ID) == f"/{title}.md"
        assert store.has_document(DOC_ID)
        doc, chunks = store.doc_chunks(DOC_ID)
        assert doc.title == title
        assert [chunk.chunk_id for chunk in chunks] == CHUNKS
        assert [chunk.text for chunk in chunks] == [f"{title} body 0", f"{title} body 1"]
        assert [chunk.path for chunk in store.chunks_by_ids(CHUNKS)] == [f"/{title}.md"] * 2
        assert store.get_entity("e1").name == f"{title} e1"
        assert store.get_entity("e1").mention_count == 1
        assert {e.entity_id for e in store.entities()} == {"e1", "e2"}
        assert store.wiki_page("e1").path == f"{title}/e1.md"
        assert store.page_by_slug("e1").title == title
        assert {p.page_id for p in store.wiki_pages()} == {"e1", "e2"}
        assert store.pages_citing_document(DOC_ID) == ["e1", "e2"]
        assert store.pages_linking_to(["e2"]) == ["e1"]
        assert {key: sorted(ids) for key, ids in store.chunk_entity_ids(DOC_ID).items()} == {
            CHUNKS[0]: ["e1", "e2"],
            CHUNKS[1]: [],
        }
        assert [c.chunk_id for c in store.chunks_mentioning("e1", list(reversed(CHUNKS)))] == [
            CHUNKS[0]
        ]
        assert store._run("MATCH (:Chunk)-[r:NEXT]->(:Chunk) RETURN count(r) AS n") == [{"n": 1}]
    scopes[1].set_document_path(DOC_ID, "/moved.md")
    assert scopes[1].document_path(DOC_ID) == "/moved.md"
    assert scopes[0].document_path(DOC_ID) == "/Legacy.md"
    assert scopes[2].document_path(DOC_ID) == "/Beta.md"


def test_team_nodes_carry_team_id_without_exposing_prefixed_local_ids(scopes):
    team = scopes[1]
    populate(team, "Alpha")
    for label, field, expected in (
        ("Document", "doc_id", [DOC_ID]),
        ("Chunk", "chunk_id", CHUNKS),
        ("Entity", "entity_id", ["e1", "e2"]),
        ("WikiPage", "page_id", ["e1", "e2"]),
    ):
        rows = team._run(f"MATCH (n:{label}) RETURN n.team_id AS team, n.{field} AS id ORDER BY id")
        assert rows == [{"team": "a" * 32, "id": key} for key in expected]


def test_evidence_relations_and_transactional_delete_do_not_touch_other_scopes(scopes):
    for store, title in zip(scopes, ("Legacy", "Alpha", "Beta"), strict=True):
        populate(store, title)
    team = scopes[1]
    for store, title in zip(scopes, ("Legacy", "Alpha", "Beta"), strict=True):
        context = store.entity_context("e1", 10)
        assert context.entity.name == f"{title} e1"
        assert len(context.relations) == 1
        assert context.relations[0].description == title
        assert context.relations[0].evidence == CHUNKS
        assert store.neighborhood(["e1"], 3, 10) == context.relations
    plan = team.document_deletion_plan(DOC_ID)
    removed = team.delete_document(DOC_ID)
    assert sorted(removed.removed_entity_ids) == sorted(plan.removed_entity_ids) == ["e1", "e2"]
    assert sorted(removed.removed_pages) == [("e1", "Alpha/e1.md"), ("e2", "Alpha/e2.md")]
    assert all(count == 0 for count in team.counts().values())
    assert team.delete_document(DOC_ID).affected == []
    for store in (scopes[0], scopes[2]):
        assert store.counts()["RELATED"] == 1
        assert store.entity_context("e1", 10).relations[0].weight == 5
        assert store.entity_context("e1", 10).relations[0].evidence == CHUNKS


def test_fulltext_limit_is_applied_within_the_team_index(scopes):
    legacy, team, other = scopes
    for store in (legacy, other):
        for i in range(40):
            store.upsert_entity(
                EntityRecord(entity_id=f"foreign-{i}", name="Needle", type="Concept")
            )
    team.upsert_entity(
        EntityRecord(entity_id="local", name="A longer Needle description", type="Concept")
    )
    assert [eid for eid, _ in team.fulltext_entities("Needle", k=1)] == ["local"]
    assert team.fulltext_entities("***", k=1) == []
    for store in (legacy, other):
        assert all(eid.startswith("foreign-") for eid, _ in store.fulltext_entities("Needle", 100))


@pytest.mark.parametrize("selected", [0, 1, 2])
def test_wipe_only_erases_the_selected_scope(scopes, selected):
    for store, title in zip(scopes, ("Legacy", "Alpha", "Beta"), strict=True):
        populate(store, title)
    scopes[selected].wipe()
    for idx, store in enumerate(scopes):
        assert store.counts()["Document"] == (0 if idx == selected else 1)
        assert store.counts()["RELATED"] == (0 if idx == selected else 1)


def test_wiki_deletion_and_link_replacement_are_scoped(scopes):
    for store, title in zip(scopes, ("Legacy", "Alpha", "Beta"), strict=True):
        populate(store, title)
    team = scopes[1]
    team.upsert_wiki_page(team.wiki_page("e1"), "e1", [], [])
    assert team.pages_linking_to(["e2"]) == []
    assert team.pages_citing_document(DOC_ID) == ["e2"]
    team.delete_wiki_page("e2")
    assert team.wiki_page("e2") is None
    for store in (scopes[0], scopes[2]):
        assert store.wiki_page("e2") is not None
        assert store.pages_linking_to(["e2"]) == ["e1"]
        assert store.pages_citing_document(DOC_ID) == ["e1", "e2"]


def test_neighborhood_ranking_ignores_accidental_edges_to_another_team(scopes):
    _, team, other = scopes
    for eid in ("seed", "local-high", "local-low", "extra"):
        team.upsert_entity(EntityRecord(entity_id=eid, name=eid, type="Concept"))
    for i in range(5):
        other.upsert_entity(EntityRecord(entity_id=f"foreign-{i}", name="Foreign", type="Concept"))
    team.merge_relation("seed", "local-high", "uses", "", 1, "evidence")
    team.merge_relation("seed", "local-low", "uses", "", 1, "evidence")
    team.merge_relation("local-high", "extra", "uses", "", 1, "evidence")
    # Simulate externally introduced cross-scope edges; application writes cannot create these.
    team._driver.execute_query(
        "MATCH (a:mb_team_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa_Entity {entity_id:'local-low'}), "
        "(b:mb_team_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb_Entity) "
        "MERGE (a)-[:RELATED {predicate:'foreign', weight:100, evidence:[], description:''}]->(b)"
    )
    assert team.neighborhood(["seed"], hops=1, limit=1)[0].dst_id == "local-high"
    assert all(
        not relation.dst_id.startswith("foreign-")
        for relation in team.neighborhood(["seed"], hops=3, limit=20)
    )
