import httpx
import pytest
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import ResponseHandlingException
from tenacity import wait_none

from mnogobase.models import ChunkRecord, EmbedInput, EntityRecord
from mnogobase.stores.qdrant_store import DimensionMismatchError, QdrantStore
from mnogobase.wiki.render import Section
from tests.fakes import FakeEmbedder, FakeSparse

pytestmark = pytest.mark.filterwarnings(
    "ignore:Payload indexes have no effect in the local Qdrant:UserWarning"
)

EMB = FakeEmbedder()
SP = FakeSparse()


def make_store(client: QdrantClient | None = None, dim: int = 64) -> QdrantStore:
    store = QdrantStore(client or QdrantClient(":memory:"), "t_", dim)
    store.ensure_collections()
    return store


def chunks(doc_id: str, texts: list[str]) -> list[ChunkRecord]:
    return [
        ChunkRecord(
            chunk_id=f"{doc_id}:{i:05d}",
            doc_id=doc_id,
            idx=i,
            text=t,
            context_text=t,
            path=f"/{doc_id}.md",
        )
        for i, t in enumerate(texts)
    ]


def index(store: QdrantStore, items: list[ChunkRecord]) -> None:
    texts = [c.context_text for c in items]
    store.upsert_chunks(
        items,
        EMB.embed_documents([EmbedInput(text=t) for t in texts]),
        SP.encode_documents(texts),
    )


def test_ensure_is_idempotent_and_checks_dimension():
    client = QdrantClient(":memory:")
    make_store(client)
    make_store(client)
    with pytest.raises(DimensionMismatchError, match="reindex"):
        QdrantStore(client, "t_", 32).ensure_collections()


def test_hybrid_search_ranks_relevant_chunk_first():
    store = make_store()
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


def test_delete_doc_only_removes_that_doc():
    store = make_store()
    index(store, chunks("aaaa", ["one", "two"]))
    index(store, chunks("bbbb", ["three"]))
    store.delete_doc("aaaa")
    assert store.client.count(store.chunks).count == 1


def test_set_doc_path_only_touches_that_doc():
    store = make_store()
    index(store, chunks("aaaa", ["one", "two"]))
    index(store, chunks("bbbb", ["three"]))
    store.set_doc_path("aaaa", "/elsewhere.md")
    points, _ = store.client.scroll(store.chunks, with_payload=True, limit=10)
    paths = {p.payload["chunk_id"]: p.payload["path"] for p in points}
    assert paths == {
        "aaaa:00000": "/elsewhere.md",
        "aaaa:00001": "/elsewhere.md",
        "bbbb:00000": "/bbbb.md",
    }


def test_chunk_entity_filter():
    store = make_store()
    index(store, chunks("aaaa", ["transformer attention", "cats and dogs"]))
    store.set_chunk_entities("aaaa:00001", ["e2", "e1", "e1"])
    hits = store.search_chunks_for_entity(EMB.embed_query("cats"), "e1", k=5)
    assert [h.key for h in hits] == ["aaaa:00001"]
    assert hits[0].payload["entity_ids"] == ["e1", "e2"]


def test_entity_search_threshold_and_delete():
    store = make_store()
    recs = [
        EntityRecord(
            entity_id="e1", name="Transformer", type="Method", description="attention architecture"
        ),
        EntityRecord(entity_id="e2", name="Cat", type="Concept", description="small animal"),
    ]
    store.upsert_entities(
        recs, EMB.embed_documents([EmbedInput(text=f"{r.name}: {r.description}") for r in recs])
    )
    hits = store.search_entities(
        EMB.embed_query("Transformer: attention architecture"), k=2, score_threshold=0.9
    )
    assert [h.key for h in hits] == ["e1"]
    assert hits[0].score > 0.99
    store.delete_entities(["e1"])
    assert store.client.count(store.entities).count == 1


def test_wiki_sections_replace_previous_version():
    store = make_store()
    sections = [
        Section("Summary", "transformer summary", ["d:00001", "d:00002"]),
        Section("Details", "attention details", []),
    ]
    texts = [s.text for s in sections]
    store.upsert_wiki_sections(
        "p1",
        "e1",
        "entities/transformer.md",
        sections,
        EMB.embed_documents([EmbedInput(text=t) for t in texts]),
        SP.encode_documents(texts),
    )
    assert store.client.count(store.wiki).count == 2
    store.upsert_wiki_sections(
        "p1",
        "e1",
        "entities/transformer.md",
        sections[:1],
        EMB.embed_documents([EmbedInput(text=texts[0])]),
        SP.encode_documents(texts[:1]),
    )
    assert store.client.count(store.wiki).count == 1
    hits = store.search_wiki(EMB.embed_query("transformer"), SP.encode_query("transformer"), k=3)
    assert hits[0].key == "p1#0"
    assert hits[0].payload["path"] == "entities/transformer.md"
    assert hits[0].payload["section"] == "Summary"
    assert hits[0].payload["chunk_ids"] == ["d:00001", "d:00002"]
    store.delete_wiki_page("p1")
    assert store.client.count(store.wiki).count == 0


class FlakyClient:
    """Proxy that fails the first `failures` calls of `method` with `exc`."""

    def __init__(self, inner: QdrantClient, method: str, exc: Exception, failures: int):
        self._inner = inner
        self._method = method
        self._exc = exc
        self.remaining = failures
        self.calls = 0

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name != self._method:
            return attr

        def flaky(*args, **kwargs):
            self.calls += 1
            if self.remaining > 0:
                self.remaining -= 1
                raise self._exc
            return attr(*args, **kwargs)

        return flaky


@pytest.mark.parametrize(
    "exc",
    [httpx.ConnectError("refused"), ResponseHandlingException(httpx.ReadTimeout("slow"))],
)
def test_transport_errors_are_retried(exc):
    flaky = FlakyClient(QdrantClient(":memory:"), "upsert", exc, failures=4)
    store = QdrantStore(flaky, "t_", 64, retry_wait=wait_none())
    store.ensure_collections()
    index(store, chunks("aaaa", ["one"]))
    assert flaky.calls == 5
    assert store.client.count(store.chunks).count == 1


def test_retry_gives_up_after_five_attempts():
    flaky = FlakyClient(QdrantClient(":memory:"), "query_points", httpx.ConnectError("x"), 5)
    store = QdrantStore(flaky, "t_", 64, retry_wait=wait_none())
    store.ensure_collections()
    with pytest.raises(httpx.ConnectError):
        store.search_entities(EMB.embed_query("x"), k=1)
    assert flaky.calls == 5


def test_non_transport_errors_are_not_retried():
    flaky = FlakyClient(QdrantClient(":memory:"), "delete", ValueError("bad request"), 1)
    store = QdrantStore(flaky, "t_", 64, retry_wait=wait_none())
    store.ensure_collections()
    with pytest.raises(ValueError, match="bad request"):
        store.delete_doc("aaaa")
    assert flaky.calls == 1


def test_get_chunks_returns_existing_payloads_by_id():
    store = make_store()
    index(store, chunks("aaaa", ["one", "two"]))
    got = store.get_chunks(["aaaa:00001", "aaaa:00009"])
    assert list(got) == ["aaaa:00001"] and got["aaaa:00001"]["text"] == "two"
    assert store.get_chunks([]) == {}
