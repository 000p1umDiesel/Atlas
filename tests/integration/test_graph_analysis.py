import pytest

from mnogobase.models import EntityRecord

pytestmark = pytest.mark.integration


def score_rows(*ids):
    return [
        {"entity_id": eid, "pagerank": float(i + 1), "community": "group"}
        for i, eid in enumerate(ids)
    ]


def test_analysis_publication_is_invalidated_by_graph_changes(graph):
    graph.upsert_entity(EntityRecord(entity_id="e1", name="First", type="Concept"))
    revision = graph.analysis_status()["revision"]
    assert graph.publish_analysis(revision, score_rows("e1"), {"gds": "test"})
    assert graph.analysis_scores(["e1"]) == {"e1": 1.0}
    graph.upsert_entity(EntityRecord(entity_id="e2", name="Second", type="Concept"))
    assert graph.analysis_status()["ready"] is False
    assert graph.analysis_scores(["e1"]) == {}
    assert not graph.publish_analysis(revision, score_rows("e1"), {})
    assert graph.analysis_scores(["e1"]) == {}


def test_partial_publication_is_rejected_without_replacing_snapshot(graph):
    for eid in ("e1", "e2"):
        graph.upsert_entity(EntityRecord(entity_id=eid, name=eid, type="Concept"))
    revision = graph.analysis_status()["revision"]
    assert graph.publish_analysis(revision, score_rows("e1", "e2"), {})
    run = graph.analysis_status()["run_id"]
    with pytest.raises(ValueError, match="complete"):
        graph.publish_analysis(revision, score_rows("e1"), {})
    assert graph.analysis_status()["run_id"] == run
    assert graph.analysis_scores(["e1", "e2"]) == {"e1": 1.0, "e2": 2.0}
    graph.clear_analysis()
    assert graph.analysis_scores(["e1"]) == {}
    assert graph.counts()["Entity"] == 2


def test_analysis_state_is_scoped_and_reset(scopes):
    # `scopes` is local to this module so duplicate local IDs are intentional.
    for i, store in enumerate(scopes):
        store.upsert_entity(EntityRecord(entity_id="same", name="Same", type="Concept"))
        assert store.publish_analysis(
            store.analysis_status()["revision"],
            [{"entity_id": "same", "pagerank": float(i + 1), "community": "g"}],
            {},
        )
    scopes[0].wipe()
    assert scopes[0].analysis_scores(["same"]) == {}
    for i, store in enumerate(scopes[1:], start=1):
        assert store.analysis_scores(["same"]) == {"same": float(i + 1)}


def test_missing_plugins_fall_back_to_cypher(graph):
    from tests.integration.test_team_graph import populate

    populate(graph, "Original")
    assert graph.plugin_status()["gds"] is None
    assert graph.neighborhood_apoc(["e1"], 2, 30) == graph.neighborhood(["e1"], 2, 30)


def test_reset_does_not_resurrect_an_old_revision(graph):
    entity = EntityRecord(entity_id="e1", name="First", type="Concept")
    graph.upsert_entity(entity)
    old_revision = graph.analysis_status()["revision"]
    graph.wipe()
    graph.ensure_schema()
    graph.upsert_entity(entity)
    assert not graph.publish_analysis(old_revision, score_rows("e1"), {})


@pytest.mark.parametrize("mutation", ["document", "chunks", "path", "mentions", "relation"])
def test_source_mutations_invalidate_published_scores(graph, mutation):
    from tests.integration.test_team_graph import CHUNKS, DOC_ID, populate

    populate(graph, "Original")
    assert graph.publish_analysis(graph.analysis_status()["revision"], score_rows("e1", "e2"), {})
    doc, chunks = graph.doc_chunks(DOC_ID)
    mutations = {
        "document": lambda: graph.upsert_document(doc),
        "chunks": lambda: graph.upsert_chunks(chunks),
        "path": lambda: graph.set_document_path(DOC_ID, "/renamed.md"),
        "mentions": lambda: graph.add_mentions(CHUNKS[0], ["e1"]),
        "relation": lambda: graph.merge_relation("e1", "e2", "new", "new edge", 1, CHUNKS[0]),
    }
    mutations[mutation]()
    assert not graph.analysis_status()["ready"]
    assert graph.analysis_scores(["e1", "e2"]) == {}


def test_failed_source_transaction_preserves_published_snapshot(graph):
    from neo4j.exceptions import ConstraintError

    graph.upsert_entity(EntityRecord(entity_id="same", name="Same", type="Concept"))
    graph.publish_analysis(graph.analysis_status()["revision"], score_rows("same"), {})
    state = graph.analysis_status()
    with pytest.raises(ConstraintError):
        graph._write("CREATE (:Entity {entity_id: 'same'})")
    assert graph.analysis_status() == state
    assert graph.analysis_scores(["same"]) == {"same": 1.0}


@pytest.fixture
def scopes(neo4j_container):
    from mnogobase.stores.graph_store import GraphStore

    stores = [
        GraphStore(neo4j_container.get_driver(), team_id=team)
        for team in (None, "a" * 32, "b" * 32)
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
