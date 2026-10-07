import shutil
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from mnogobase.app import build_app
from mnogobase.config import ChunkingSettings, WikiSettings, load_settings
from mnogobase.maintenance import reindex, reset
from mnogobase.pipeline import EmbedderMismatchError
from mnogobase.stores.qdrant_store import QdrantStore
from tests.fakes import FakeEmbedder, FakeLLM, FakeSparse, scripted_llm_handler

pytestmark = [
    pytest.mark.integration,
    pytest.mark.filterwarnings(
        "ignore:Payload indexes have no effect in the local Qdrant:UserWarning"
    ),
]
FIXTURES = Path(__file__).parents[1] / "fixtures"


@pytest.fixture
def docs(tmp_path) -> Path:
    folder = tmp_path / "docs"
    folder.mkdir()
    for name in ("attention_en.md", "vnimanie_ru.md"):
        shutil.copy(FIXTURES / name, folder / name)
    return folder


@pytest.fixture
def settings(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("{}\n", encoding="utf-8")
    return load_settings(config).model_copy(
        update={
            "data_dir": tmp_path / ".mb",
            "logs_dir": tmp_path / "logs",
            "wiki": WikiSettings(dir=tmp_path / "wiki", min_mentions=1),
            "chunking": ChunkingSettings(max_tokens=128),
        }
    )


def make_app(settings, graph, client: QdrantClient, embedder: FakeEmbedder):
    return build_app(
        settings,
        embedder=embedder,
        sparse=FakeSparse(),
        llm=FakeLLM(scripted_llm_handler),
        vectors=QdrantStore(client, "t_", embedder.dim),
        graph=graph,
    )


def chunk_payloads(app) -> dict[str, dict]:
    points, _ = app.vectors.client.scroll(app.vectors.chunks, with_payload=True, limit=10_000)
    return {p.payload["chunk_id"]: p.payload for p in points}


async def test_reindex_after_embedder_change_then_reset(graph, tmp_path, docs, settings):
    client = QdrantClient(":memory:")

    first = make_app(settings, graph, client, FakeEmbedder(64))
    await first.pipeline.ingest([docs])
    first.registry.close()

    embedder = FakeEmbedder(32)
    embedder.model_id = "fake-embed-v2"
    second = make_app(settings, graph, client, embedder)
    with pytest.raises(EmbedderMismatchError):
        second.pipeline.prepare()
    stats = reindex(second)
    assert stats["chunks"] > 0 and stats["entities"] > 0 and stats["wiki_sections"] > 0
    assert second.vectors.collection_dim(second.vectors.chunks) == 32
    second.pipeline.prepare()  # signature now matches
    assert second.vectors.search_chunks(
        second.embedder.embed_query("attention"), second.sparse.encode_query("attention"), k=3
    )

    reset(second)
    assert graph.counts()["Entity"] == 0
    assert not (tmp_path / "wiki").exists()
    assert second.registry.files() == []
    assert not client.collection_exists(second.vectors.chunks)
    second.registry.close()


async def test_reindex_without_chunk_cache_uses_graph_chunks(graph, docs, settings):
    client = QdrantClient(":memory:")
    app = make_app(settings, graph, client, FakeEmbedder(64))
    await app.pipeline.ingest([docs])
    before = chunk_payloads(app)
    assert any(p["entity_ids"] for p in before.values())
    shutil.rmtree(settings.data_dir / "cache")  # chunk caches gone

    stats = reindex(app)

    after = chunk_payloads(app)
    assert stats["chunks"] == len(before) and after.keys() == before.keys()
    for chunk_id, payload in before.items():
        for key in ("doc_id", "text", "path", "page", "headings", "entity_ids"):
            assert after[chunk_id][key] == payload[key], (chunk_id, key)
    app.registry.close()


async def test_interrupted_reindex_must_be_rerun(graph, docs, settings):
    client = QdrantClient(":memory:")
    app = make_app(settings, graph, client, FakeEmbedder(64))
    await app.pipeline.ingest([docs])
    app.registry.close()

    class BrokenEmbedder(FakeEmbedder):
        def embed_documents(self, items):
            raise RuntimeError("embedder went away")

    broken = make_app(settings, graph, client, BrokenEmbedder(64))
    with pytest.raises(RuntimeError, match="embedder went away"):
        reindex(broken)
    # same embedder, but the half-built index must not be silently accepted
    with pytest.raises(EmbedderMismatchError, match="reindex"):
        broken.pipeline.prepare()
    broken.registry.close()

    healed = make_app(settings, graph, client, FakeEmbedder(64))
    assert reindex(healed)["chunks"] > 0
    healed.pipeline.prepare()  # a completed reindex records the signature again
    healed.registry.close()
