import uuid
from collections.abc import Iterator

import pytest
from qdrant_client import QdrantClient

from mnogobase.stores.qdrant_store import QdrantStore
from tests.fakes import FakeEmbedder, FakeSparse
from tests.unit.test_qdrant_store import chunks, index

pytestmark = pytest.mark.integration

EMB = FakeEmbedder()
SP = FakeSparse()


@pytest.fixture
def store(qdrant_client: QdrantClient) -> Iterator[QdrantStore]:
    store = QdrantStore(qdrant_client, f"it_{uuid.uuid4().hex[:8]}_", EMB.dim)
    store.ensure_collections()
    try:
        yield store
    finally:
        store.drop_collections()


def test_server_has_payload_indexes(store: QdrantStore):
    schema = store.client.get_collection(store.chunks).payload_schema
    assert {"chunk_id", "doc_id", "entity_ids", "modality"} <= set(schema)
    assert store.collection_dim(store.chunks) == EMB.dim


def test_hybrid_search_ranks_relevant_chunk_first(store: QdrantStore):
    index(
        store,
        chunks(
            "aaaa",
            [
                "the transformer uses attention",
                "cats are cute animals",
                "softmax normalizes scores",
            ],
        ),
    )
    q = "attention transformer"
    hits = store.search_chunks(EMB.embed_query(q), SP.encode_query(q), k=2)
    assert len(hits) == 2
    assert hits[0].key == "aaaa:00000"
    assert hits[0].payload["path"] == "/aaaa.md"
    assert hits[0].payload["entity_ids"] == []


def test_chunk_entity_filter(store: QdrantStore):
    index(store, chunks("aaaa", ["transformer attention", "cats and dogs"]))
    store.set_chunk_entities("aaaa:00001", ["e2", "e1", "e1"])
    hits = store.search_chunks_for_entity(EMB.embed_query("cats"), "e1", k=5)
    assert [h.key for h in hits] == ["aaaa:00001"]
    assert hits[0].payload["entity_ids"] == ["e1", "e2"]
