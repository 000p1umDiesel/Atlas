import pytest

from mnogobase.core.models import EntityRecord
from tests.integration.graph_seed import add_mentions, merge_relation

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
    with pytest.raises(ValueError, match="все сущности"):
        graph.publish_analysis(revision, score_rows("e1"), {})
    assert graph.analysis_status()["run_id"] == run
    assert graph.analysis_scores(["e1", "e2"]) == {"e1": 1.0, "e2": 2.0}
    graph.clear_analysis()
    assert graph.analysis_scores(["e1"]) == {}
    assert graph.counts()["Entity"] == 2


def test_missing_plugins_fall_back_to_cypher(graph):
    from tests.integration.graph_seed import populate

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
    from tests.integration.graph_seed import CHUNKS, DOC_ID, populate

    populate(graph, "Original")
    assert graph.publish_analysis(graph.analysis_status()["revision"], score_rows("e1", "e2"), {})
    doc, chunks = graph.doc_chunks(DOC_ID)
    mutations = {
        "document": lambda: graph.upsert_document(doc),
        "chunks": lambda: graph.upsert_chunks(chunks),
        "path": lambda: graph.set_document_path(DOC_ID, "/renamed.md"),
        "mentions": lambda: add_mentions(graph, CHUNKS[0], ["e1"]),
        "relation": lambda: merge_relation(graph, "e1", "e2", "new", "new edge", 1, CHUNKS[0]),
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
