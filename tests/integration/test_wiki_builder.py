from types import SimpleNamespace

import pytest
from qdrant_client import QdrantClient
from qdrant_client import models as qm

from mnogobase.config import WikiSettings
from mnogobase.models import ChunkRecord, DocumentRecord, EmbedInput, EntityRecord
from mnogobase.registry import Registry
from mnogobase.stores.qdrant_store import QdrantStore
from mnogobase.wiki.builder import WikiBuilder
from tests.fakes import FakeEmbedder, FakeLLM, FakeSparse, scripted_llm_handler

pytestmark = [
    pytest.mark.integration,
    pytest.mark.filterwarnings(
        "ignore:Payload indexes have no effect in the local Qdrant:UserWarning"
    ),
]
DOC = "d" * 16


@pytest.fixture
def world(graph, tmp_path):
    def factory(min_mentions: int = 2, handler=scripted_llm_handler):
        vectors = QdrantStore(QdrantClient(":memory:"), "t_", 64)
        vectors.ensure_collections()
        emb, sparse = FakeEmbedder(), FakeSparse()
        graph.upsert_document(
            DocumentRecord(
                doc_id=DOC, path="/docs/attention.pdf", title="T", mime="application/pdf"
            )
        )
        texts = [
            "The Transformer uses attention.",
            "Softmax normalizes attention scores.",
            "Transformer layers stack attention and softmax.",
        ]
        chunks = [
            ChunkRecord(
                chunk_id=f"{DOC}:{i:05d}",
                doc_id=DOC,
                idx=i,
                text=t,
                context_text=t,
                page_start=i + 1,
                path="/docs/attention.pdf",
            )
            for i, t in enumerate(texts)
        ]
        graph.upsert_chunks(chunks)
        vectors.upsert_chunks(
            chunks,
            emb.embed_documents([EmbedInput(text=t) for t in texts]),
            sparse.encode_documents(texts),
        )
        graph.upsert_entity(
            EntityRecord(
                entity_id="e1",
                name="Transformer",
                type="Method",
                description="Architecture.",
                descriptions=["Architecture."],
            )
        )
        graph.upsert_entity(
            EntityRecord(
                entity_id="e2",
                name="Softmax",
                type="Concept",
                aliases=["софтмакс"],
                description="Function.",
                descriptions=["Function."],
            )
        )
        for chunk, ids in ((chunks[0], ["e1"]), (chunks[1], ["e2"]), (chunks[2], ["e1", "e2"])):
            graph.add_mentions(chunk.chunk_id, ids)
            vectors.set_chunk_entities(chunk.chunk_id, ids)
        graph.merge_relation("e1", "e2", "uses", "Transformer uses softmax.", 5, chunks[2].chunk_id)
        registry = Registry(tmp_path / "state.db")
        registry.mark_dirty(["e1", "e2"])
        llm = FakeLLM(handler)
        wiki_dir = tmp_path / "wiki"
        builder = WikiBuilder(
            WikiSettings(dir=wiki_dir, min_mentions=min_mentions),
            graph,
            vectors,
            emb,
            sparse,
            llm,
            registry,
        )
        return SimpleNamespace(
            builder=builder,
            vectors=vectors,
            registry=registry,
            llm=llm,
            wiki=wiki_dir,
            chunks=chunks,
        )

    return factory


def wiki_points(vectors: QdrantStore, page_id: str) -> list[dict]:
    points, _ = vectors.client.scroll(
        vectors.wiki,
        scroll_filter=qm.Filter(
            must=[qm.FieldCondition(key="page_id", match=qm.MatchValue(value=page_id))]
        ),
        with_payload=True,
        limit=100,
    )
    return [p.payload for p in points]


async def test_build_creates_linked_cited_pages(world, graph):
    w = world()
    report = await w.builder.build(run_id="r1", documents=["/docs/attention.pdf"])
    assert sorted(report.created) == ["Softmax", "Transformer"]
    assert report.failed == []
    text = (w.wiki / "entities" / "transformer.md").read_text(encoding="utf-8")
    assert "# Transformer" in text
    assert "[[softmax|Softmax]]" in text
    assert "Nonexistent Thing" in text and "[[Nonexistent Thing]]" not in text
    assert "## Sources" in text and "*attention.pdf*, p." in text
    assert graph.wiki_page("e1").version == 1
    links = graph._run("MATCH (:WikiPage {page_id:'e1'})-[:LINKS_TO]->(p) RETURN p.page_id AS id")
    assert [r["id"] for r in links] == ["e2"]
    assert w.vectors.client.count(w.vectors.wiki).count >= 2
    assert "[[transformer|Transformer]]" in (w.wiki / "index.md").read_text(encoding="utf-8")
    log = (w.wiki / "log.md").read_text(encoding="utf-8")
    assert "run r1" in log and "/docs/attention.pdf" in log
    assert w.registry.dirty() == []


async def test_rebuild_updates_existing_page(world, graph):
    w = world()
    await w.builder.build(run_id="r1")
    w.registry.mark_dirty(["e1"])
    report = await w.builder.build(run_id="r2")
    assert report.updated == ["Transformer"] and report.created == []
    assert graph.wiki_page("e1").version == 2
    last_prompt = [p for p in w.llm.calls_for("wiki") if '"Transformer"' in p][-1]
    assert "Summary sentence." in last_prompt  # existing body was passed back to the LLM


async def test_entities_below_threshold_are_skipped(world):
    w = world(min_mentions=3)
    report = await w.builder.build()
    assert report.created == [] and report.updated == []
    assert w.registry.dirty() == []
    assert not (w.wiki / "entities").exists()


async def test_failed_entity_does_not_stop_the_build(world, graph):
    def handler(task: str, prompt: str) -> str:
        if task == "wiki" and '"Softmax"' in prompt:
            raise RuntimeError("llm down")
        return scripted_llm_handler(task, prompt)

    w = world(handler=handler)
    report = await w.builder.build(run_id="r1")
    assert report.created == ["Transformer"]
    assert report.failed == ["Softmax"]
    assert (w.wiki / "entities" / "transformer.md").exists()
    assert not (w.wiki / "entities" / "softmax.md").exists()
    assert graph.wiki_page("e2") is None
    assert w.registry.dirty() == ["e2"]


async def test_entity_without_evidence_is_not_linked(world, graph):
    w = world()
    # Softmax is mentioned in the graph, but no chunk vector carries it -> no evidence, no page
    w.vectors.set_chunk_entities(w.chunks[1].chunk_id, [])
    w.vectors.set_chunk_entities(w.chunks[2].chunk_id, ["e1"])
    report = await w.builder.build()
    assert report.created == ["Transformer"] and report.skipped == ["Softmax"]
    text = (w.wiki / "entities" / "transformer.md").read_text(encoding="utf-8")
    assert "[[softmax|" not in text
    assert "See Softmax and" in text  # the LLM link became plain text
    assert "- uses → Softmax" in text  # Related lists it without a link
    links = graph._run("MATCH (:WikiPage {page_id:'e1'})-[:LINKS_TO]->(p) RETURN p.page_id AS id")
    assert links == []
    assert w.registry.dirty() == []


@pytest.mark.parametrize("change", ["below_threshold", "entity_removed"])
async def test_page_of_dropped_entity_is_deleted(world, graph, change):
    w = world()
    await w.builder.build(run_id="r1")
    assert wiki_points(w.vectors, "e2")
    if change == "below_threshold":
        graph._run("MATCH (e:Entity {entity_id: 'e2'}) SET e.mention_count = 1")
    else:
        graph._run("MATCH (e:Entity {entity_id: 'e2'}) DETACH DELETE e")
    w.registry.mark_dirty(["e2"])
    report = await w.builder.build(run_id="r2", deleted=["Gone Elsewhere"])
    assert report.deleted == ["Gone Elsewhere", "Softmax"]
    assert report.created == [] and report.updated == []
    assert not (w.wiki / "entities" / "softmax.md").exists()
    assert graph.wiki_page("e2") is None
    assert wiki_points(w.vectors, "e2") == []
    assert "Softmax" not in (w.wiki / "index.md").read_text(encoding="utf-8")
    assert "deleted (2): Gone Elsewhere, Softmax" in (w.wiki / "log.md").read_text(encoding="utf-8")
    assert w.registry.dirty() == []


async def test_same_slug_entities_get_distinct_pages(world, graph):
    w = world()
    for eid, name in (("e3", "C#"), ("e4", "CSharp")):
        graph.upsert_entity(EntityRecord(entity_id=eid, name=name, type="Language"))
    for chunk, ids in ((w.chunks[0], ["e1", "e3", "e4"]), (w.chunks[2], ["e1", "e2", "e3", "e4"])):
        graph.add_mentions(chunk.chunk_id, ids)
        w.vectors.set_chunk_entities(chunk.chunk_id, ids)
    w.registry.mark_dirty(["e3", "e4"])
    report = await w.builder.build()
    assert sorted(report.created) == ["C#", "CSharp", "Softmax", "Transformer"]
    assert graph.wiki_page("e3").slug == "csharp"
    assert graph.wiki_page("e4").slug == "csharp-language"
    assert (w.wiki / "entities" / "csharp.md").read_text(encoding="utf-8").count("# C#\n") == 1
    assert "# CSharp\n" in (w.wiki / "entities" / "csharp-language.md").read_text(encoding="utf-8")
    again = await w.builder.build(rebuild_all=True)
    assert sorted(again.updated) == ["C#", "CSharp", "Softmax", "Transformer"]
    assert graph.wiki_page("e3").slug == "csharp"
    assert graph.wiki_page("e4").slug == "csharp-language"


async def test_index_page_stores_cited_chunk_ids(world):
    w = world()
    await w.builder.build()
    payloads = wiki_points(w.vectors, "e1")
    summary = next(p for p in payloads if p["section"] == "Summary")
    assert summary["chunk_ids"] and set(summary["chunk_ids"]) <= {c.chunk_id for c in w.chunks}
    assert all(p["path"] == "entities/transformer.md" for p in payloads)
    # reindex path: re-embed a page from its file
    text = (w.wiki / "entities" / "transformer.md").read_text(encoding="utf-8")
    count = w.builder.index_page("e1", "e1", "entities/transformer.md", text)
    assert count == len(payloads)
    assert sorted(p["key"] for p in wiki_points(w.vectors, "e1")) == sorted(
        p["key"] for p in payloads
    )
