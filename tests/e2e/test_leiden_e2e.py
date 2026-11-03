"""Real Leiden/APOC -> source expansion -> configured LLM API with a cited secret fact."""

import json
from pathlib import Path

import pytest

from mnogobase.config import GraphSettings, load_settings
from mnogobase.embedding.openai_compat import OpenAICompatEmbedder
from mnogobase.graph_analysis import analyze
from mnogobase.llm.client import OpenAICompatLLM
from mnogobase.models import ChunkRecord, DocumentRecord, EntityRecord
from mnogobase.retrieval.answer import Answerer
from mnogobase.retrieval.combined import CombinedRetriever
from mnogobase.retrieval.graph import GraphRetriever
from mnogobase.stores.graph_store import GraphStore
from mnogobase.stores.qdrant_store import QdrantStore
from tests.integration.test_graph_plugins import plugin_container as plugin_container

pytestmark = pytest.mark.e2e
ROOT = Path(__file__).parents[2]


@pytest.mark.parametrize("mode", ["graph", "all"])
async def test_leiden_evidence_reaches_real_api(plugin_container, qdrant_client, tmp_path, mode):
    settings = load_settings(ROOT / "config.yaml")
    assert settings.llm.serve == "api", "Use the configured API, never a local LLM substitute"
    store = GraphStore(plugin_container.get_driver(), team_id="c" * 32)
    store.wipe()
    store.ensure_schema()
    embedder = OpenAICompatEmbedder(settings.embedder)
    llm = OpenAICompatLLM(settings.llm.model_copy(update={"concurrency": 2, "timeout_s": 90}))
    try:
        vectors = QdrantStore(qdrant_client, f"leiden_{mode}_", lambda: embedder.dim)
        vectors.ensure_collections()
        store.upsert_document(
            DocumentRecord(
                doc_id="proof",
                path="/fixtures/community-proof.md",
                title="Leiden proof",
                mime="text/markdown",
            )
        )
        ids = ("seed", "hub", "remote", "aux1", "aux2", "aux3")
        for eid in ids:
            store.upsert_entity(
                EntityRecord(
                    entity_id=eid, name="LeafSeed" if eid == "seed" else eid, type="Concept"
                )
            )
        for i, eid in enumerate(("seed", "remote", "aux1", "aux2", "aux3")):
            cid = f"proof:{i:05d}"
            text = (
                "The archive access code for this topic is ORCHID-734."
                if eid == "remote"
                else f"{eid} belongs to the same topic centered on hub."
            )
            store.upsert_chunks(
                [
                    ChunkRecord(
                        chunk_id=cid,
                        doc_id="proof",
                        idx=i,
                        text=text,
                        context_text=text,
                        page_start=i + 1,
                    )
                ]
            )
            store.add_mentions(cid, [eid, "hub"])
            store.merge_relation(eid, "hub", "belongs", "Topic membership", 5, cid)
        report = analyze(store, settings.graph.analysis)
        memberships = {r["entity_id"]: r["community"] for r in store.analysis_details()}
        assert memberships["seed"] == memberships["remote"], memberships
        graph_settings = GraphSettings(
            traversal="apoc",
            use_gds=True,
            hops=1,
            seeds=1,
            seed_threshold=0.99,
            community={"enabled": True, "max_chunks": 2},
        )
        retriever = GraphRetriever(store, vectors, embedder, graph_settings)
        question = "What is the archive access code associated with LeafSeed's topic?"
        graph_settings.community.enabled = False
        baseline = retriever.retrieve(question, 4)
        assert not any("ORCHID-734" in i.text for i in baseline)
        graph_settings.community.enabled = True
        selected = (
            retriever
            if mode == "graph"
            else CombinedRetriever(
                {"graph": retriever}, {"graph": 0.6}, settings.retrieval.context_tokens
            )
        )
        items = selected.retrieve(question, 4)
        assert any(i.ref == "proof:00001" and "ORCHID-734" in i.text for i in items)
        answer = await Answerer(llm, settings.retrieval.context_tokens, "en").answer(
            question, mode, items
        )
        assert "ORCHID-734" in answer.text, answer.text
        assert any(
            s.ref == "proof:00001"
            and s.cited
            and s.page == 2
            and s.path == "/fixtures/community-proof.md"
            for s in answer.sources
        )
        assert answer.tokens_in > 0 and answer.tokens_out > 0
        (tmp_path / "leiden-answer.json").write_text(
            json.dumps(
                {
                    "llm_model": settings.llm.model,
                    "analysis": report,
                    "baseline_chunk_ids": [i.ref for i in baseline if i.kind == "chunk"],
                    "expanded_chunk_ids": [i.ref for i in items if i.kind == "chunk"],
                    "answer": answer.model_dump(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        store.wipe()
        store.close()
        embedder.close()
        await llm.aclose()
