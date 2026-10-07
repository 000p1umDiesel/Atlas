import logging

import pytest

from mnogobase.models import ChunkRecord, DocumentRecord, EntityRecord, WikiPageRecord
from mnogobase.stores.graph_store import DeleteResult

pytestmark = pytest.mark.integration


def make_doc(graph, doc_id: str = "d" * 16, n: int = 3) -> list[ChunkRecord]:
    graph.upsert_document(
        DocumentRecord(doc_id=doc_id, path=f"/{doc_id}.md", title="T", mime="text/markdown")
    )
    chunks = [
        ChunkRecord(
            chunk_id=f"{doc_id}:{i:05d}",
            doc_id=doc_id,
            idx=i,
            text=f"text {i}",
            context_text=f"text {i}",
            page_start=i + 1,
        )
        for i in range(n)
    ]
    graph.upsert_chunks(chunks)
    return chunks


def make_entity(graph, eid: str, name: str, etype: str = "Concept", aliases=()) -> EntityRecord:
    rec = EntityRecord(
        entity_id=eid,
        name=name,
        type=etype,
        aliases=list(aliases),
        description=f"{name} desc",
        descriptions=[f"{name} desc"],
    )
    graph.upsert_entity(rec)
    return rec


def test_document_chunks_next_and_idempotency(graph):
    chunks = make_doc(graph)
    views = sorted(graph.chunks_by_ids([c.chunk_id for c in chunks]), key=lambda v: v.chunk_id)
    assert [v.chunk_id for v in views] == [c.chunk_id for c in chunks]
    assert views[0].path == f"/{'d' * 16}.md" and views[0].page == 1
    n_next = graph._run("MATCH (:Chunk)-[r:NEXT]->(:Chunk) RETURN count(r) AS n")[0]["n"]
    assert n_next == 2
    make_doc(graph)
    assert graph.counts()["Chunk"] == 3


def test_mentions_and_relations_are_idempotent(graph):
    chunks = make_doc(graph)
    make_entity(graph, "e1", "Transformer")
    make_entity(graph, "e2", "Softmax")
    graph.add_mentions(chunks[0].chunk_id, ["e1", "e2"])
    graph.add_mentions(chunks[1].chunk_id, ["e1"])
    graph.add_mentions(chunks[1].chunk_id, ["e1"])
    assert graph.get_entity("e1").mention_count == 2
    graph.merge_relation("e1", "e2", "uses", "T uses softmax", 5, chunks[0].chunk_id)
    graph.merge_relation("e1", "e2", "uses", "T uses softmax", 5, chunks[0].chunk_id)
    graph.merge_relation(
        "e1", "e2", "uses", "Transformer uses softmax in attention", 3, chunks[1].chunk_id
    )
    rels = graph.entity_context("e1", max_relations=10).relations
    assert len(rels) == 1
    assert rels[0].weight == 8
    assert sorted(rels[0].evidence) == [chunks[0].chunk_id, chunks[1].chunk_id]
    assert rels[0].description == "Transformer uses softmax in attention"
    assert graph.chunk_entity_ids("d" * 16)[chunks[0].chunk_id] in (["e1", "e2"], ["e2", "e1"])


def test_delete_document_cascade(graph):
    a = make_doc(graph, "a" * 16, 2)
    b = make_doc(graph, "b" * 16, 1)
    make_entity(graph, "e1", "Shared")
    make_entity(graph, "e2", "Only A")
    make_entity(graph, "e3", "Only B")
    graph.add_mentions(a[0].chunk_id, ["e1", "e2"])
    graph.add_mentions(b[0].chunk_id, ["e1", "e3"])
    graph.merge_relation("e1", "e2", "uses", "", 4, a[0].chunk_id)
    # e1-uses->e3 carries evidence from both documents: a's entry first, then b's
    graph.merge_relation("e1", "e3", "uses", "", 3, a[0].chunk_id)
    graph.merge_relation("e1", "e3", "uses", "", 4, b[0].chunk_id)
    graph.merge_relation("e1", "e3", "cites", "", 2, a[1].chunk_id)
    graph.upsert_wiki_page(
        WikiPageRecord(page_id="e2", slug="only-a", title="Only A", path="entities/only-a.md"),
        "e2",
        [],
        [a[0].chunk_id],
    )

    plan = graph.document_deletion_plan("a" * 16)
    assert graph.counts()["Document"] == 2  # the plan is read-only
    result = graph.delete_document("a" * 16)
    for field in ("affected", "removed_entity_ids", "removed_names", "removed_pages"):
        # the plan predicts exactly what the cascade removes
        assert sorted(getattr(plan, field)) == sorted(getattr(result, field)), field
    assert graph.document_deletion_plan("a" * 16) == DeleteResult([], [], [], [])

    assert sorted(result.affected) == ["e1", "e2"]
    assert result.removed_entity_ids == ["e2"]
    assert result.removed_names == ["Only A"]
    assert result.removed_pages == [("e2", "entities/only-a.md")]
    assert graph.get_entity("e2") is None
    assert graph.get_entity("e1").mention_count == 1
    rels = graph.entity_context("e1", max_relations=10).relations
    assert [(r.predicate, r.dst_id) for r in rels] == [("uses", "e3")]
    assert rels[0].evidence == [b[0].chunk_id]
    assert rels[0].weight == 4
    strengths = graph._run(
        "MATCH (:Entity {entity_id:'e1'})-[r:RELATED {predicate:'uses'}]->(:Entity {entity_id:'e3'}) "
        "RETURN r.strengths AS s"
    )
    assert strengths == [{"s": [4]}]
    assert graph.counts()["Document"] == 1
    assert graph.wiki_page("e2") is None


def test_unknown_type_notifications_are_not_logged(graph, caplog):
    with caplog.at_level(logging.DEBUG, logger="neo4j.notifications"):
        graph._run("MATCH ()-[r:NEVER_CREATED_TYPE]->() RETURN count(r) AS n")
    assert not [r for r in caplog.records if r.name.startswith("neo4j.notifications")]


def test_fulltext_special_characters(graph):
    make_entity(graph, "e1", "C++", aliases=["cpp"])
    make_entity(graph, "e2", "Transformer")
    for query in ["C++ AND (foo)", 'what is "transformer"?', "a/b: c~ OR NOT", "***", ""]:
        graph.fulltext_entities(query, k=5)  # must not raise
    assert [eid for eid, _ in graph.fulltext_entities("tell me about the transformer", 5)] == ["e2"]
    assert "e1" in [eid for eid, _ in graph.fulltext_entities("cpp", 5)]


def test_neighborhood_hops_and_ranking(graph):
    chunks = make_doc(graph)
    for i in range(1, 5):
        make_entity(graph, f"e{i}", f"Entity {i}")
    graph.merge_relation("e1", "e2", "r", "", 9, chunks[0].chunk_id)
    graph.merge_relation("e2", "e3", "r", "", 2, chunks[0].chunk_id)
    graph.merge_relation("e3", "e4", "r", "", 5, chunks[0].chunk_id)
    two = graph.neighborhood(["e1"], hops=2, limit=10)
    assert {(r.src_id, r.dst_id) for r in two} == {("e1", "e2"), ("e2", "e3")}
    assert two[0].weight == 9
    one = graph.neighborhood(["e1"], hops=1, limit=10)
    assert [(r.src_id, r.dst_id) for r in one] == [("e1", "e2")]


def test_wiki_page_links_are_replaced(graph):
    chunks = make_doc(graph)
    for eid, name in (("e1", "One"), ("e2", "Two"), ("e3", "Three")):
        make_entity(graph, eid, name, etype="Method")
        graph.upsert_wiki_page(
            WikiPageRecord(
                page_id=eid, slug=name.lower(), title=name, path=f"entities/{name.lower()}.md"
            ),
            eid,
            [],
            [],
        )
    page = WikiPageRecord(page_id="e1", slug="one", title="One", path="entities/one.md", version=2)
    graph.upsert_wiki_page(page, "e1", ["e2"], [chunks[0].chunk_id])
    graph.upsert_wiki_page(page, "e1", ["e3"], [chunks[1].chunk_id])
    links = graph._run("MATCH (:WikiPage {page_id:'e1'})-[:LINKS_TO]->(p) RETURN p.page_id AS id")
    cites = graph._run("MATCH (:WikiPage {page_id:'e1'})-[:CITES]->(c) RETURN c.chunk_id AS id")
    assert [r["id"] for r in links] == ["e3"]
    assert [r["id"] for r in cites] == [chunks[1].chunk_id]
    assert graph.wiki_page("e1").version == 2
    assert graph.page_by_slug("two").page_id == "e2"
    rows = graph.wiki_pages()
    assert {r.page_id for r in rows} == {"e1", "e2", "e3"}
    assert all(r.entity_type == "Method" for r in rows)


def test_delete_wiki_page_keeps_entity(graph):
    make_doc(graph)
    for eid, name in (("e1", "One"), ("e2", "Two")):
        make_entity(graph, eid, name)
        page = WikiPageRecord(page_id=eid, slug=name.lower(), title=name, path=f"{name}.md")
        graph.upsert_wiki_page(page, eid, [], [])
    graph.upsert_wiki_page(graph.wiki_page("e1"), "e1", ["e2"], [])
    graph.delete_wiki_page("e2")
    assert graph.wiki_page("e2") is None
    assert graph.get_entity("e2") is not None
    assert [r.page_id for r in graph.wiki_pages()] == ["e1"]
    graph.delete_wiki_page("missing")  # no-op


def test_pages_linking_to(graph):
    make_doc(graph)
    for eid, name in (("e1", "One"), ("e2", "Two"), ("e3", "Three"), ("e4", "Four")):
        make_entity(graph, eid, name)
        page = WikiPageRecord(page_id=eid, slug=name.lower(), title=name, path=f"{name}.md")
        graph.upsert_wiki_page(page, eid, [], [])
    for src, targets in (("e1", ["e2", "e3"]), ("e2", ["e3"]), ("e3", ["e2"])):
        graph.upsert_wiki_page(graph.wiki_page(src), src, targets, [])
    assert graph.pages_linking_to(["e3"]) == ["e1", "e2"]
    # the queried pages themselves are excluded: they are the ones going away
    assert graph.pages_linking_to(["e2", "e3"]) == ["e1"]
    assert graph.pages_linking_to(["e4"]) == []
    assert graph.pages_linking_to([]) == []


def test_doc_chunks_rebuilds_records_from_the_graph(graph):
    doc_id = "e" * 16
    graph.upsert_document(
        DocumentRecord(doc_id=doc_id, path="/docs/e.md", title="E", mime="text/markdown")
    )
    chunks = [
        ChunkRecord(
            chunk_id=f"{doc_id}:{i:05d}",
            doc_id=doc_id,
            idx=i,
            text=f"body {i}",
            context_text=f"Intro\nbody {i}" if i else f"body {i}",
            headings=["Intro"] if i else [],
            page_start=i + 1,
            page_end=i + 2,
            n_tokens=7,
        )
        for i in range(12)  # idx 10/11 sort after 9, not after 1
    ]
    graph.upsert_chunks(list(reversed(chunks)))
    make_doc(graph)  # another document's chunks stay out

    doc, rebuilt = graph.doc_chunks(doc_id)
    assert doc == DocumentRecord(doc_id=doc_id, path="/docs/e.md", title="E", mime="text/markdown")
    expected = [c.model_copy(update={"path": "/docs/e.md"}) for c in chunks]
    assert rebuilt == expected
    assert graph.doc_chunks("0" * 16) is None
    graph.upsert_document(
        DocumentRecord(doc_id="f" * 16, path="/f.md", title="F", mime="text/markdown")
    )
    assert graph.doc_chunks("f" * 16)[1] == []


def test_chunks_mentioning_keeps_existing_mentioning_chunks_in_order(graph):
    chunks = make_doc(graph, n=3)
    make_entity(graph, "e1", "Transformer")
    graph.add_mentions(chunks[0].chunk_id, ["e1"])
    graph.add_mentions(chunks[2].chunk_id, ["e1"])
    asked = [chunks[2].chunk_id, chunks[1].chunk_id, "gone:00000", chunks[0].chunk_id]
    views = graph.chunks_mentioning("e1", asked)
    assert [v.chunk_id for v in views] == [chunks[2].chunk_id, chunks[0].chunk_id]
    assert views[0].path == f"/{'d' * 16}.md" and views[0].page == 3
    assert graph.chunks_mentioning("e1", []) == []
