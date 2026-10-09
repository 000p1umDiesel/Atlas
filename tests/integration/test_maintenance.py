import shutil
import uuid
from pathlib import Path

import pytest
from qdrant_client import QdrantClient
from typer.testing import CliRunner

from mnogobase.app import build_app
from mnogobase.config import ChunkingSettings, WikiSettings, load_settings
from mnogobase.embedding.base import embedder_signature
from mnogobase.maintenance import reindex, reset
from mnogobase.pipeline import EmbedderMismatchError
from mnogobase.stores.qdrant_store import QdrantStore
from tests.fakes import (
    FakeEmbedder,
    FakeLLM,
    FakeSparse,
    RecordingProgress,
    scripted_llm_handler,
)

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
    second.pipeline.prepare()  # теперь сигнатура совпадает
    assert second.vectors.search_chunks(
        second.embedder.embed_query("attention"), second.sparse.encode_query("attention"), k=3
    )

    assert second.registry.get_meta("embedder") == embedder_signature(embedder)
    assert ":tpl-" in second.registry.get_meta("embedder")  # новый формат, шаблоны в сигнатуре

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
    shutil.rmtree(settings.data_dir / "cache")  # кэши чанков удалены

    stats = reindex(app)

    after = chunk_payloads(app)
    assert stats["chunks"] == len(before) and after.keys() == before.keys()
    for chunk_id, payload in before.items():
        for key in ("doc_id", "text", "path", "page", "headings", "entity_ids"):
            assert after[chunk_id][key] == payload[key], (chunk_id, key)
    app.registry.close()


async def test_reindex_reports_progress(graph, docs, settings):
    client = QdrantClient(":memory:")
    app = make_app(settings, graph, client, FakeEmbedder(64))
    await app.pipeline.ingest([docs])
    progress = RecordingProgress()
    reindex(app, progress=progress)
    entities, pages = len(app.graph.entities()), len(app.graph.wiki_pages())
    assert progress.steps() == [
        ("step", "reindex chunks", 2),  # документы
        ("step", "reindex entities", entities),
        ("step", "reindex wiki", pages),
    ]
    assert progress.advanced("reindex chunks") == [("advance", 1, False)] * 2
    assert sum(n for _, n, _ in progress.advanced("reindex entities")) == entities
    assert progress.advanced("reindex wiki") == [("advance", 1, False)] * pages
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
    # эмбеддер тот же, но недостроенный индекс нельзя молча принимать
    with pytest.raises(EmbedderMismatchError, match="reindex"):
        broken.pipeline.prepare()
    broken.registry.close()

    healed = make_app(settings, graph, client, FakeEmbedder(64))
    assert reindex(healed)["chunks"] > 0
    healed.pipeline.prepare()  # завершённый reindex снова записывает сигнатуру
    healed.registry.close()


@pytest.fixture
def cli_project(tmp_path, monkeypatch, qdrant_url, neo4j_container, graph, docs):
    """Проект, управляемый через CLI: настоящие сервер Qdrant и Neo4j, фейковые модели.

    Отдаёт `write_config(dim)`; preflight выполняет настоящую проверку `qdrant` (сервисы,
    которые заменены фейками, помечаются как ok)."""
    from mnogobase import cli
    from mnogobase.doctor import Check, run_checks
    from mnogobase.stores.graph_store import DRIVER_OPTIONS, GraphStore

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "configure_logging", lambda *args, **kwargs: None)
    prefix = f"it_{uuid.uuid4().hex[:8]}_"

    def write_config(dim: int, doc_template: str = "{text}") -> None:
        (tmp_path / "config.yaml").write_text(
            f"data_dir: .mb\nqdrant:\n  url: {qdrant_url}\n  prefix: {prefix}\n"
            f"embedder:\n  dim: {dim}\n  doc_template: '{doc_template}'\n"
            "wiki:\n  dir: wiki\n  min_mentions: 1\nchunking:\n  max_tokens: 128\n",
            encoding="utf-8",
        )

    def preflight(settings, only=None, **kwargs):
        real = run_checks(settings, only=[n for n in only if n == "qdrant"], **kwargs)
        return real + [Check(n, True, "stub") for n in only if n != "qdrant"]

    def app_with_fakes(settings):
        return build_app(
            settings,
            embedder=FakeEmbedder(
                settings.embedder.dim,
                (settings.embedder.doc_template, settings.embedder.query_template),
            ),
            sparse=FakeSparse(),
            llm=FakeLLM(scripted_llm_handler),
            graph=GraphStore(neo4j_container.get_driver(**DRIVER_OPTIONS)),
        )

    monkeypatch.setattr(cli, "run_checks", preflight)
    monkeypatch.setattr(cli, "build_app", app_with_fakes)
    yield write_config
    QdrantStore(QdrantClient(url=qdrant_url), prefix, 1).drop_collections()


def test_cli_reindex_rebuilds_the_index_after_a_dimension_change(cli_project, tmp_path):
    from mnogobase import cli

    runner = CliRunner()
    cli_project(64)
    built = runner.invoke(cli.app, ["ingest", "docs"])
    assert built.exit_code == 0, built.output

    cli_project(32)  # например, MRL-усечение: embedder.dim 768 -> 512
    refused = runner.invoke(cli.app, ["ingest", "docs"])
    assert refused.exit_code == 2, refused.output
    assert "has dim 64, config 32" in refused.output and "mnogobase reindex" in refused.output

    result = runner.invoke(cli.app, ["reindex"])
    assert result.exit_code == 0, result.output
    assert "reindexed:" in result.output

    settings = load_settings(tmp_path / "config.yaml")
    app = cli.build_app(settings)
    try:
        for name in (app.vectors.chunks, app.vectors.entities, app.vectors.wiki):
            assert app.vectors.collection_dim(name) == 32
        app.pipeline.prepare()  # сохранённая сигнатура уже от нового эмбеддера
        hits = app.vectors.search_chunks(
            app.embedder.embed_query("attention"), app.sparse.encode_query("attention"), k=3
        )
        assert hits
    finally:
        app.close()
    again = runner.invoke(cli.app, ["ingest", "docs"])
    assert again.exit_code == 0, again.output
    assert "skipped 2" in again.output


def test_cli_template_change_requires_reindex(cli_project, tmp_path):
    from mnogobase import cli

    runner = CliRunner()
    cli_project(64)
    built = runner.invoke(cli.app, ["ingest", "docs"])
    assert built.exit_code == 0, built.output

    cli_project(64, doc_template="passage: {text}")  # та же модель и dim, новый шаблон
    for args in (["ingest", "docs"], ["ask", "what is attention?"]):
        refused = runner.invoke(cli.app, args)
        assert refused.exit_code == 2, refused.output
        assert "templates changed" in refused.output, refused.output
        assert "mnogobase reindex" in refused.output

    result = runner.invoke(cli.app, ["reindex"])
    assert result.exit_code == 0, result.output
    settings = load_settings(tmp_path / "config.yaml")
    app = cli.build_app(settings)
    try:
        assert app.registry.get_meta("embedder") == embedder_signature(app.embedder)
    finally:
        app.close()
    answered = runner.invoke(cli.app, ["ask", "what is attention?"])
    assert answered.exit_code == 0, answered.output


def test_cli_reindex_interrupted_at_a_new_dimension_can_be_rerun(
    cli_project, tmp_path, monkeypatch
):
    from mnogobase import cli
    from mnogobase.stores.graph_store import GraphStore

    runner = CliRunner()
    cli_project(64)
    assert runner.invoke(cli.app, ["ingest", "docs"]).exit_code == 0
    cli_project(32)

    real = GraphStore.entities
    state = {"failed": False}

    def entities_once(self, *args, **kwargs):  # падение после повторного эмбеддинга чанков
        if not state["failed"]:
            state["failed"] = True
            raise RuntimeError("reindex interrupted")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(GraphStore, "entities", entities_once)
    crashed = runner.invoke(cli.app, ["reindex"])
    assert crashed.exit_code != 0 and state["failed"]

    refused = runner.invoke(cli.app, ["ingest", "docs"])
    assert refused.exit_code == 2, refused.output
    assert "reindex-in-progress" in refused.output

    result = runner.invoke(cli.app, ["reindex"])  # preflight проходит, повторный запуск завершается
    assert result.exit_code == 0, result.output
    assert "reindexed:" in result.output
    settings = load_settings(tmp_path / "config.yaml")
    app = cli.build_app(settings)
    try:
        for name in (app.vectors.chunks, app.vectors.entities, app.vectors.wiki):
            assert app.vectors.collection_dim(name) == 32
    finally:
        app.close()
    again = runner.invoke(cli.app, ["ingest", "docs"])
    assert again.exit_code == 0, again.output
    assert "skipped 2" in again.output
