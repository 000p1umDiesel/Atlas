import pytest
from qdrant_client import QdrantClient

from mnogobase.config import GraphSettings
from mnogobase.models import ChunkRecord, DocumentRecord, EmbedInput, EntityRecord
from mnogobase.retrieval.graph import GraphRetriever
from mnogobase.stores.qdrant_store import QdrantStore
from tests.fakes import FakeEmbedder

pytestmark = [
    pytest.mark.integration,
    pytest.mark.filterwarnings(
        "ignore:Payload indexes have no effect in the local Qdrant:UserWarning"
    ),
]


def test_graph_retriever_returns_entities_relations_and_evidence(graph):
    emb = FakeEmbedder()
    vectors = QdrantStore(QdrantClient(":memory:"), "t_", 64)
    vectors.ensure_collections()
    graph.upsert_document(
        DocumentRecord(doc_id="d" * 16, path="/docs/d.md", title="D", mime="text/markdown")
    )
    chunk = ChunkRecord(
        chunk_id=f"{'d' * 16}:00000",
        doc_id="d" * 16,
        idx=0,
        text="The Transformer uses softmax.",
        context_text="x",
    )
    graph.upsert_chunks([chunk])
    entities = [
        EntityRecord(
            entity_id="e1", name="Transformer", type="Method", description="Attention architecture."
        ),
        EntityRecord(
            entity_id="e2", name="Softmax", type="Concept", description="Normalizing function."
        ),
    ]
    for e in entities:
        graph.upsert_entity(e)
    vectors.upsert_entities(
        entities,
        emb.embed_documents([EmbedInput(text=f"{e.name}: {e.description}") for e in entities]),
    )
    graph.add_mentions(chunk.chunk_id, ["e1", "e2"])
    graph.merge_relation("e1", "e2", "uses", "Transformer uses softmax.", 5, chunk.chunk_id)

    # порог 0.9 отключает нечёткий векторный поиск стартовых узлов, они берутся
    # только из fulltext-индекса
    retriever = GraphRetriever(graph, vectors, emb, GraphSettings(seed_threshold=0.9))
    items = retriever.retrieve("What is the Transformer?", 5)
    kinds = [i.kind for i in items]
    assert "entity" in kinds and "relation" in kinds and "chunk" in kinds
    entity = next(i for i in items if i.kind == "entity")
    assert entity.text.startswith("Transformer (Method)")
    relation = next(i for i in items if i.kind == "relation")
    assert relation.text.startswith("Transformer —uses→ Softmax")
    evidence = next(i for i in items if i.kind == "chunk")
    assert evidence.ref == chunk.chunk_id and evidence.path == "/docs/d.md"
    assert retriever.retrieve("zzz qqq", 5) == []
