import shutil
from pathlib import Path

import pytest
from qdrant_client import QdrantClient
from qdrant_client import models as qm
from structlog.testing import capture_logs

from mnogobase.app import build_app
from mnogobase.config import ChunkingSettings, WikiSettings, load_settings
from mnogobase.ids import entity_id, file_doc_id
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
def make_app(graph, tmp_path):
    apps = []
    config = tmp_path / "config.yaml"
    config.write_text("{}\n", encoding="utf-8")

    def factory(handler=scripted_llm_handler):
        settings = load_settings(config).model_copy(
            update={
                "data_dir": tmp_path / ".mb",
                "logs_dir": tmp_path / "logs",
                "wiki": WikiSettings(dir=tmp_path / "wiki", min_mentions=1),
                "chunking": ChunkingSettings(max_tokens=128),
            }
        )
        vectors = QdrantStore(QdrantClient(":memory:"), "t_", 64)
        app = build_app(
            settings,
            embedder=FakeEmbedder(),
            sparse=FakeSparse(),
            llm=FakeLLM(handler),
            vectors=vectors,
            graph=graph,
        )
        apps.append(app)
        return app

    yield factory
    for app in apps:
        app.registry.close()


def doc_filter(doc_id: str) -> qm.Filter:
    return qm.Filter(must=[qm.FieldCondition(key="doc_id", match=qm.MatchValue(value=doc_id))])


def chunk_count(app, doc_id: str) -> int:
    return app.vectors.client.count(app.vectors.chunks, count_filter=doc_filter(doc_id)).count


def chunk_paths(app, doc_id: str) -> set[str]:
    points, _ = app.vectors.client.scroll(
        app.vectors.chunks, scroll_filter=doc_filter(doc_id), with_payload=True, limit=1000
    )
    return {p.payload["path"] for p in points}


def document_path(app, doc_id: str) -> str:
    rows = app.graph._run("MATCH (d:Document {doc_id: $id}) RETURN d.path AS path", id=doc_id)
    return rows[0]["path"]


async def test_ingest_builds_vectors_graph_and_wiki(make_app, docs, tmp_path):
    app = make_app()
    report = await app.pipeline.ingest([docs])
    assert report.failed == {}
    assert len(report.processed) == 2
    assert app.graph.counts()["Document"] == 2
    attention = app.graph.get_entity(entity_id("Method", "Attention Mechanism"))
    assert attention is not None and attention.mention_count >= 2  # RU and EN merged
    assert "механизм внимания" in attention.aliases
    assert (tmp_path / "wiki" / "entities" / "attention-mechanism.md").exists()
    assert report.wiki is not None and "Attention Mechanism" in report.wiki.created
    assert report.wiki.failed == []
    hits = app.vectors.search_chunks_for_entity(
        app.embedder.embed_query("attention"), attention.entity_id, k=10
    )
    assert hits
    log = (tmp_path / "wiki" / "log.md").read_text(encoding="utf-8")
    assert all(path in log for path in report.processed)


async def test_reingest_is_noop(make_app, docs):
    app = make_app()
    await app.pipeline.ingest([docs])
    calls = len(app.llm.calls_for("extract"))
    report = await app.pipeline.ingest([docs])
    assert report.processed == [] and len(report.skipped) == 2
    assert len(app.llm.calls_for("extract")) == calls


async def test_changed_file_replaces_old_chunks(make_app, docs):
    app = make_app()
    await app.pipeline.ingest([docs])
    path = docs / "attention_en.md"
    old = file_doc_id(path)
    path.write_text(
        path.read_text(encoding="utf-8") + "\n\n## Extra\n\nSoftmax again.\n", encoding="utf-8"
    )
    report = await app.pipeline.ingest([docs])
    assert report.processed == [str(path.resolve())]
    assert chunk_count(app, old) == 0
    assert chunk_count(app, file_doc_id(path)) > 0
    assert app.graph.counts()["Document"] == 2


async def test_duplicate_content_survives_change(make_app, docs):
    app = make_app()
    # sorts before attention_en.md, so the shared document is first recorded under this path
    copy = docs / "a_copy.md"
    shutil.copy(docs / "attention_en.md", copy)
    await app.pipeline.ingest([docs])
    shared = file_doc_id(copy)
    assert document_path(app, shared) == str(copy.resolve())
    copy.write_text("# Different\n\nSomething about softmax.\n", encoding="utf-8")
    report = await app.pipeline.ingest([docs])
    assert report.failed == {}
    assert chunk_count(app, shared) > 0  # still referenced by attention_en.md
    assert app.graph.counts()["Document"] == 3
    survivor = str((docs / "attention_en.md").resolve())
    assert document_path(app, shared) == survivor  # citations point at a file that has it
    assert chunk_paths(app, shared) == {survivor}
    doc, chunks = app.pipeline.load_chunks(shared)
    assert doc.path == survivor and {c.path for c in chunks} == {survivor}


async def test_empty_document(make_app, tmp_path):
    folder = tmp_path / "empty_docs"
    folder.mkdir()
    (folder / "empty.md").write_text("", encoding="utf-8")
    app = make_app()
    report = await app.pipeline.ingest([folder])
    assert report.failed == {}
    assert len(report.processed) == 1
    assert app.graph.counts()["Chunk"] == 0


async def test_extraction_failure_then_retry(make_app, docs):
    def broken(task, prompt):
        if task == "extract":
            raise RuntimeError("llm down")
        return scripted_llm_handler(task, prompt)

    app = make_app(broken)
    with capture_logs() as logs:
        report = await app.pipeline.ingest([docs], build_wiki=False)
    assert len(report.failed) == 2
    assert all("ExtractionFailedError" in msg for msg in report.failed.values())
    failures = [e for e in logs if e["event"] == "ingest_file_failed"]
    assert len(failures) == 2
    assert all(e["exc_info"] and e["error_type"] == "ExtractionFailedError" for e in failures)
    doc_id = file_doc_id(docs / "attention_en.md")
    assert app.registry.stage_status(doc_id, "extract") == "failed"
    calls = len(app.llm.calls_for("extract"))

    again = await app.pipeline.ingest([docs], build_wiki=False)
    assert len(again.failed) == 2 and "--retry-failed" in next(iter(again.failed.values()))
    assert len(app.llm.calls_for("extract")) == calls

    app.llm.handler = scripted_llm_handler
    fixed = await app.pipeline.ingest([docs], build_wiki=False, retry_failed=True)
    assert fixed.failed == {} and len(fixed.processed) == 2


async def test_partial_extraction_is_done_with_warning(make_app, tmp_path):
    folder = tmp_path / "partial"
    folder.mkdir()
    sections = "\n\n".join(f"## Part {i}\n\nThe Transformer, part {i}." for i in range(8))
    path = folder / "parts.md"
    path.write_text(f"# Parts\n\n{sections}\n\n## Odd\n\nSoftmax only here.\n", encoding="utf-8")

    def flaky(task, prompt):
        if task == "extract" and "Softmax only here" in prompt:
            raise RuntimeError("llm hiccup")
        return scripted_llm_handler(task, prompt)

    app = make_app(flaky)
    with capture_logs() as logs:
        report = await app.pipeline.ingest([folder], build_wiki=False)
    assert report.failed == {} and report.processed == [str(path.resolve())]
    _doc, chunks = app.pipeline.load_chunks(file_doc_id(path))
    assert len(chunks) >= 5  # one failure stays within max_failed_ratio=0.2
    partial = [e for e in logs if e["event"] == "extract_partial"]
    assert len(partial) == 1
    assert partial[0]["failed"] == 1 and partial[0]["total"] == len(chunks)
    assert partial[0]["log_level"] == "warning"


async def test_graph_stage_rerun_is_idempotent(make_app, docs):
    app = make_app()
    await app.pipeline.ingest([docs], build_wiki=False)
    before = app.graph.counts()
    doc_id = file_doc_id(docs / "attention_en.md")
    app.registry.set_stage(doc_id, "graph", "running")  # simulate a crash during the graph stage
    report = await app.pipeline.ingest([docs], build_wiki=False)
    assert report.processed == [str((docs / "attention_en.md").resolve())]
    assert app.graph.counts() == before


async def test_removed_page_leaves_no_dangling_links(make_app, tmp_path):
    folder = tmp_path / "linked"
    folder.mkdir()
    gone = folder / "gone.md"
    gone.write_text("# Notes\n\nSoftmax turns scores into probabilities.\n", encoding="utf-8")
    (folder / "kept.md").write_text("# Models\n\nThe Transformer is a network.\n", encoding="utf-8")
    app = make_app()
    first = await app.pipeline.ingest([folder])
    assert sorted(first.wiki.created) == ["Softmax", "Transformer"]
    page = tmp_path / "wiki" / "entities" / "transformer.md"
    assert "[[softmax|Softmax]]" in page.read_text(encoding="utf-8")

    # Softmax lives only in gone.md; Transformer's page is not about anything in it
    gone.write_text("# Notes\n\nNothing to see here.\n", encoding="utf-8")
    report = await app.pipeline.ingest([folder])
    assert report.processed == [str(gone.resolve())]
    assert report.wiki.deleted == ["Softmax"]
    assert report.wiki.updated == ["Transformer"]
    assert "[[softmax|" not in page.read_text(encoding="utf-8")
    assert not (tmp_path / "wiki" / "entities" / "softmax.md").exists()
    assert app.registry.dirty() == []


async def test_embedder_mismatch_is_refused(make_app):
    app = make_app()
    app.registry.set_meta("embedder", "other-model:1024")
    with pytest.raises(EmbedderMismatchError, match="reindex"):
        app.pipeline.prepare()


async def test_graph_stage_uses_extractions_of_a_previous_model(make_app, docs, monkeypatch):
    app = make_app()

    async def graph_down(doc_id):
        raise RuntimeError("neo4j down")

    monkeypatch.setattr(app.pipeline, "_build_graph", graph_down)
    first = await app.pipeline.ingest([docs], build_wiki=False)
    assert len(first.failed) == 2
    assert app.graph.counts()["Entity"] == 0
    monkeypatch.undo()
    calls = len(app.llm.calls_for("extract"))

    # the extraction model changes before the failed graph stage is retried
    app.llm.model_for = lambda task: f"other-{task}"
    retried = await app.pipeline.ingest([docs], build_wiki=False, retry_failed=True)
    assert retried.failed == {} and len(retried.processed) == 2
    assert len(app.llm.calls_for("extract")) == calls  # cached results reused, no new calls
    attention = app.graph.get_entity(entity_id("Method", "Attention Mechanism"))
    assert attention is not None and attention.mention_count >= 2


async def test_graph_stage_fails_when_extractions_are_missing(make_app, docs):
    app = make_app()
    await app.pipeline.ingest([docs], build_wiki=False)
    path = docs / "attention_en.md"
    doc_id = file_doc_id(path)
    app.registry._db.execute("DELETE FROM extraction_cache WHERE chunk_id LIKE ?", (f"{doc_id}:%",))
    app.registry.set_stage(doc_id, "graph", "pending")  # e.g. interrupted before it finished

    report = await app.pipeline.ingest([docs], build_wiki=False)
    assert list(report.failed) == [str(path.resolve())]
    assert "ExtractionFailedError" in report.failed[str(path.resolve())]
    assert app.registry.stage_status(doc_id, "graph") == "failed"
    assert app.registry.stage_status(doc_id, "extract") == "pending"  # retry re-extracts

    calls = len(app.llm.calls_for("extract"))
    fixed = await app.pipeline.ingest([docs], build_wiki=False, retry_failed=True)
    assert fixed.failed == {} and fixed.processed == [str(path.resolve())]
    assert len(app.llm.calls_for("extract")) > calls
