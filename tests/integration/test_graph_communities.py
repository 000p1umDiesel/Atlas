"""Current-snapshot Leiden memberships select scoped, cited source text."""

import pytest

from mnogobase.config import GraphSettings
from mnogobase.models import ChunkRecord, DocumentRecord, EntityRecord
from mnogobase.retrieval.graph import GraphRetriever
from tests.fakes import FakeEmbedder
from tests.integration.graph_seed import add_mentions, merge_relation

pytestmark = [
    pytest.mark.integration,
    pytest.mark.filterwarnings(
        "ignore:Payload indexes have no effect in the local Qdrant:UserWarning"
    ),
]


def populate_community(store, name="Alpha"):
    store.upsert_document(
        DocumentRecord(doc_id="d", path=f"/{name}.md", title=name, mime="text/markdown")
    )
    for i, eid in enumerate(("seed", "near", "remote", "other")):
        store.upsert_entity(EntityRecord(entity_id=eid, name=f"{name} {eid}", type="Concept"))
        store.upsert_chunks(
            [
                ChunkRecord(
                    chunk_id=f"d:{i}",
                    doc_id="d",
                    idx=i,
                    text=f"{name} source for {eid}",
                    context_text="",
                    page_start=i + 1,
                )
            ]
        )
        add_mentions(store, f"d:{i}", [eid])
    merge_relation(store, "seed", "near", "uses", "local relation", 5, "d:1")
    rows = [
        {"entity_id": eid, "pagerank": rank, "community": group}
        for eid, rank, group in (
            ("seed", 1.0, "group"),
            ("near", 2.0, "group"),
            ("remote", 8.0, "group"),
            ("other", 9.0, "elsewhere"),
        )
    ]
    assert store.publish_analysis(store.analysis_status()["revision"], rows, {})


def test_community_chunks_select_current_members_with_limits_and_original_citations(graph):
    populate_community(graph)
    chunks = graph.community_chunks(["seed"], 2, max_communities=1, max_entities=2)
    assert [c.chunk_id for c in chunks] == ["d:2", "d:1"]
    assert [(c.path, c.page) for c in chunks] == [("/Alpha.md", 3), ("/Alpha.md", 2)]
    assert graph.community_chunks(["unknown"], 4) == []
    assert graph.community_chunks([], 4) == []
    assert graph.community_chunks(["seed"], 0) == []
    assert [
        c.chunk_id
        for c in graph.community_chunks(["seed"], 3, max_entities=2, exclude_chunk_ids=["d:2"])
    ] == ["d:1"]
    graph.upsert_entity(EntityRecord(entity_id="extra", name="New", type="Concept"))
    assert graph.community_chunks(["seed"], 4) == []


def test_community_chunks_respect_seed_group_priority_and_clear(graph):
    populate_community(graph)
    assert [
        c.chunk_id
        for c in graph.community_chunks(
            ["seed", "other", "seed"], 3, max_communities=1, max_entities=1
        )
    ] == ["d:2"]
    graph.clear_analysis()
    assert graph.community_chunks(["seed"], 3) == []


@pytest.mark.parametrize(
    "k,expected",
    [(0, []), (1, ["d:1"]), (2, ["d:1", "d:2"]), (3, ["d:1", "d:2"]), (8, ["d:1", "d:2", "d:0"])],
)
def test_retrieval_expands_beyond_hops_without_losing_local_evidence(graph, k, expected):
    from qdrant_client import QdrantClient

    from mnogobase.stores.qdrant_store import QdrantStore

    populate_community(graph)
    client = QdrantClient(":memory:")
    try:
        vectors = QdrantStore(client, "communities_", 64)
        vectors.ensure_collections()
        settings = GraphSettings(
            use_gds=True, hops=1, seed_threshold=0.99, seeds=1, community={"enabled": True}
        )
        retriever = GraphRetriever(graph, vectors, FakeEmbedder(), settings)
        # Exact deterministic seed selection; all graph traversal and chunk selection is real.
        graph.fulltext_entities = lambda *a: [("seed", 1.0)]
        items = retriever.retrieve("seed", k)
        assert [i.ref for i in items if i.kind == "chunk"] == expected
        if k >= 2:
            assert items[0].kind == "chunk"
        settings.community.enabled = False
        assert [i.ref for i in retriever.retrieve("seed", k) if i.kind == "chunk"] == (
            ["d:1"] if k else []
        )
    finally:
        client.close()


def test_community_evidence_works_without_relations_and_is_ignored_when_stale(graph):
    from types import SimpleNamespace

    from mnogobase.retrieval.combined import CombinedRetriever

    populate_community(graph)
    # Seed "other" has a community and MENTIONS evidence, but no RELATED edges.
    graph.fulltext_entities = lambda *a: [("other", 1.0)]
    vectors = SimpleNamespace(search_entities=lambda *a, **kw: [])
    settings = GraphSettings(use_gds=True, community={"enabled": True})
    retriever = GraphRetriever(graph, vectors, FakeEmbedder(), settings)
    combined = CombinedRetriever({"graph": retriever}, {"graph": 1.0}, 100)
    assert [i.ref for i in combined.retrieve("other", 1) if i.kind == "chunk"] == ["d:3"]
    settings.use_gds = False
    assert not any(i.kind == "chunk" for i in retriever.retrieve("other", 2))
    settings.use_gds = True
    graph.delete_document("d")
    assert not any(i.kind == "chunk" for i in retriever.retrieve("other", 2))
