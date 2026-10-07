import json

import pytest
from qdrant_client import QdrantClient

from mnogobase.config import Settings
from mnogobase.models import ChunkRecord, ChunkView, ContextItem, EmbedInput
from mnogobase.retrieval import Mode, build_retrievers
from mnogobase.retrieval.answer import NO_SOURCES, Answerer, build_context
from mnogobase.retrieval.combined import CombinedRetriever
from mnogobase.retrieval.compare import compare
from mnogobase.retrieval.graph import GraphRetriever
from mnogobase.retrieval.rag import RagRetriever
from mnogobase.retrieval.wiki import WikiRetriever
from mnogobase.stores.qdrant_store import QdrantStore
from mnogobase.wiki.render import Section
from tests.fakes import FakeEmbedder, FakeLLM, FakeSparse, scripted_llm_handler

pytestmark = pytest.mark.filterwarnings(
    "ignore:Payload indexes have no effect in the local Qdrant:UserWarning"
)


class Stub:
    def __init__(self, items):
        self.items = items

    def retrieve(self, query, k):
        return self.items[:k]


class StubGraph:
    """Only `chunks_by_ids`, the one GraphStore call the wiki retriever makes."""

    def __init__(self, chunks: list[ChunkView]):
        self.chunks = {c.chunk_id: c for c in chunks}
        self.requested: list[list[str]] = []

    def chunks_by_ids(self, chunk_ids):
        self.requested.append(list(chunk_ids))
        return [self.chunks[c] for c in chunk_ids if c in self.chunks]


def item(kind, ref, text="x" * 40, path=None, page=None) -> ContextItem:
    return ContextItem(kind=kind, ref=ref, text=text, path=path, page=page)


def test_build_context_numbers_and_budget():
    items = [
        item("chunk", "d:00001", "alpha " * 10, path="/d/a.pdf", page=2),
        item("wiki", "p#0", "beta", path="entities/x.md"),
        item("relation", "r", "A —uses→ B"),
    ]
    context, sources, used = build_context(items, 1000)
    assert context.startswith("[1] (document a.pdf, p.2)\nalpha")
    assert "[2] (wiki entities/x.md)\nbeta" in context
    assert "[3] (graph relation)\nA —uses→ B" in context
    assert [s.n for s in sources] == [1, 2, 3] and used > 0
    _, few, _ = build_context(items, 1)
    assert len(few) == 1  # at least one source even if over budget


def test_combined_dedupes_and_respects_budget():
    shared = item("chunk", "c1")
    parts = {
        "rag": Stub([shared, item("chunk", "c2")]),
        "wiki": Stub([item("wiki", "p#0")]),
        "graph": Stub([item("entity", "e1"), shared, item("relation", "r1")]),
    }
    budget = {"rag": 0.5, "wiki": 0.25, "graph": 0.25}
    refs = [(i.kind, i.ref) for i in CombinedRetriever(parts, budget, 1000).retrieve("q", 5)]
    assert refs == [
        ("chunk", "c1"),
        ("chunk", "c2"),
        ("wiki", "p#0"),
        ("entity", "e1"),
        ("relation", "r1"),
    ]
    tight = CombinedRetriever(parts, budget, 20).retrieve("q", 5)
    assert [(i.kind, i.ref) for i in tight] == [("chunk", "c1")]


def test_combined_skips_an_oversized_item_but_keeps_smaller_later_ones():
    parts = {"rag": Stub([item("chunk", "big", "x" * 400), item("chunk", "small", "x" * 8)])}
    picked = CombinedRetriever(parts, {"rag": 1.0}, 20).retrieve("q", 5)
    assert [i.ref for i in picked] == ["small"]


async def test_answerer_marks_cited_sources():
    llm = FakeLLM(lambda task, prompt: "Because of X [2].")
    answer = await Answerer(llm, 1000).answer(
        "Why?", "rag", [item("chunk", "c1"), item("chunk", "c2")]
    )
    assert answer.text == "Because of X [2]."
    assert [s.cited for s in answer.sources] == [False, True]
    assert answer.mode == "rag" and answer.tokens_in > 0 and answer.context_tokens > 0
    assert "Question: Why?" in llm.calls_for("answer")[0]


async def test_answerer_without_sources_skips_llm():
    llm = FakeLLM(scripted_llm_handler)
    answer = await Answerer(llm, 1000).answer("Why?", "graph", [])
    assert answer.text == NO_SOURCES and llm.calls == []


async def test_compare_writes_jsonl(tmp_path):
    retrievers = {m: Stub([item("chunk", f"{m}1")]) for m in ("rag", "wiki", "graph", "all")}
    answers = await compare(
        "Q?",
        retrievers,
        Answerer(FakeLLM(scripted_llm_handler), 1000),
        k=3,
        runs_dir=tmp_path,
        config_hash="abc",
    )
    assert [a.mode for a in answers] == ["rag", "wiki", "graph", "all"]
    lines = (tmp_path / "compare.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    first = json.loads(lines[0])
    assert set(first) == {
        "ts",
        "question",
        "mode",
        "answer",
        "sources",
        "latency_ms",
        "tokens_in",
        "tokens_out",
        "context_tokens",
        "config_hash",
    }
    assert first["config_hash"] == "abc" and first["question"] == "Q?" and first["sources"]
    assert first["mode"] == "rag" and first["answer"] == answers[0].text
    assert first["sources"][0]["ref"] == "rag1" and first["sources"][0]["cited"] is True


def _stores():
    emb, sparse = FakeEmbedder(), FakeSparse()
    vectors = QdrantStore(QdrantClient(":memory:"), "t_", 64)
    vectors.ensure_collections()
    return emb, sparse, vectors


def test_rag_and_wiki_retrievers():
    emb, sparse, vectors = _stores()
    texts = ["the transformer uses attention", "cats are cute"]
    chunks = [
        ChunkRecord(
            chunk_id=f"aaaa:{i:05d}",
            doc_id="aaaa",
            idx=i,
            text=t,
            context_text=t,
            page_start=1,
            path="/docs/a.pdf",
        )
        for i, t in enumerate(texts)
    ]
    vectors.upsert_chunks(
        chunks,
        emb.embed_documents([EmbedInput(text=t) for t in texts]),
        sparse.encode_documents(texts),
    )
    text = "Transformer — Summary\ntransformer attention architecture"
    section = [Section("Summary", text, ["aaaa:00000", "aaaa:00099"])]
    vectors.upsert_wiki_sections(
        "p1",
        "e1",
        "entities/transformer.md",
        section,
        emb.embed_documents([EmbedInput(text=text)]),
        sparse.encode_documents([text]),
    )
    rag = RagRetriever(vectors, emb, sparse).retrieve("transformer attention", 2)
    assert rag[0].kind == "chunk" and rag[0].ref == "aaaa:00000"
    assert rag[0].path == "/docs/a.pdf" and rag[0].page == 1

    graph = StubGraph(
        [ChunkView(chunk_id="aaaa:00000", doc_id="aaaa", text=texts[0], path="/docs/a.pdf", page=1)]
    )
    wiki = WikiRetriever(vectors, emb, sparse, graph).retrieve("transformer", 2)
    assert wiki[0].kind == "wiki" and wiki[0].ref == "p1#0"
    assert wiki[0].path == "entities/transformer.md"
    # the section's cited chunks follow as citable evidence (an unknown id is simply skipped)
    assert graph.requested == [["aaaa:00000", "aaaa:00099"]]
    assert [(i.kind, i.ref, i.path, i.page, i.text) for i in wiki[1:]] == [
        ("chunk", "aaaa:00000", "/docs/a.pdf", 1, texts[0])
    ]


def test_wiki_retriever_dedupes_and_caps_cited_chunks():
    emb, sparse, vectors = _stores()
    sections = [
        Section("Summary", "transformer summary", ["d:00001", "d:00002"]),
        Section("Details", "transformer details", ["d:00002", "d:00003"]),
        Section("Other", "cats are cute", ["d:00009"]),
    ]
    texts = [s.text for s in sections]
    vectors.upsert_wiki_sections(
        "p1",
        "e1",
        "entities/transformer.md",
        sections,
        emb.embed_documents([EmbedInput(text=t) for t in texts]),
        sparse.encode_documents(texts),
    )
    graph = StubGraph(
        [ChunkView(chunk_id=f"d:0000{i}", doc_id="d", text=f"c{i}") for i in (1, 2, 3)]
    )
    items = WikiRetriever(vectors, emb, sparse, graph).retrieve("transformer", 2)
    # each section is followed by its own cited chunks: de-duplicated, at most k in total
    # (the first section's two citations already use the cap of k=2)
    assert [i.kind for i in items] == ["wiki", "chunk", "chunk", "wiki"]
    assert {items[0].ref, items[3].ref} == {"p1#0", "p1#1"}
    cited = {"p1#0": ["d:00001", "d:00002"], "p1#1": ["d:00002", "d:00003"]}
    expected = cited[items[0].ref]
    assert graph.requested == [expected]
    assert [i.ref for i in items[1:3]] == expected


def test_wiki_retriever_interleaves_cited_chunks_after_their_section():
    emb, sparse, vectors = _stores()
    sections = [
        Section("Summary", "transformer summary", ["d:00001"]),
        Section("Details", "transformer details", ["d:00001", "d:00002"]),
    ]
    texts = [s.text for s in sections]
    vectors.upsert_wiki_sections(
        "p1",
        "e1",
        "entities/transformer.md",
        sections,
        emb.embed_documents([EmbedInput(text=t) for t in texts]),
        sparse.encode_documents(texts),
    )
    graph = StubGraph([ChunkView(chunk_id=f"d:0000{i}", doc_id="d", text=f"c{i}") for i in (1, 2)])
    items = WikiRetriever(vectors, emb, sparse, graph).retrieve("transformer", 5)
    cited = {"p1#0": ["d:00001"], "p1#1": ["d:00001", "d:00002"]}
    first, second = items[0].ref, next(i.ref for i in items[1:] if i.kind == "wiki")
    expected = [("wiki", first)] + [("chunk", c) for c in cited[first]]
    expected += [("wiki", second)]
    expected += [("chunk", c) for c in cited[second] if c not in cited[first]]
    assert [(i.kind, i.ref) for i in items] == expected
    assert len(graph.requested) == 1  # still one graph round trip


def test_wiki_retriever_skips_graph_without_cited_chunks():
    emb, sparse, vectors = _stores()
    graph = StubGraph([])
    retriever = WikiRetriever(vectors, emb, sparse, graph)
    assert retriever.retrieve("transformer", 3) == []
    section = [Section("Summary", "transformer summary", [])]
    vectors.upsert_wiki_sections(
        "p1",
        "e1",
        "entities/transformer.md",
        section,
        emb.embed_documents([EmbedInput(text=section[0].text)]),
        sparse.encode_documents([section[0].text]),
    )
    assert [i.kind for i in retriever.retrieve("transformer", 3)] == ["wiki"]
    assert graph.requested == []


def test_build_retrievers_wires_every_mode():
    emb, sparse, vectors = _stores()
    graph = StubGraph([])
    retrievers = build_retrievers(vectors, graph, emb, sparse, Settings())
    assert list(retrievers) == [m.value for m in Mode] == ["rag", "wiki", "graph", "all"]
    assert isinstance(retrievers["rag"], RagRetriever)
    assert isinstance(retrievers["wiki"], WikiRetriever)
    assert isinstance(retrievers["graph"], GraphRetriever)
    assert isinstance(retrievers["all"], CombinedRetriever)
    assert retrievers["wiki"]._graph is graph
