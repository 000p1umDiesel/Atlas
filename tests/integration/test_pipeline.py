import json
import shutil
from pathlib import Path

import pytest
from qdrant_client import QdrantClient
from qdrant_client import models as qm
from structlog.testing import capture_logs

from mnogobase.app import build_app
from mnogobase.config import ChunkingSettings, ExtractSettings, WikiSettings, load_settings
from mnogobase.embedding.base import embedder_signature
from mnogobase.ids import entity_id, file_doc_id
from mnogobase.maintenance import reset
from mnogobase.pipeline import EmbedderMismatchError, PendingRemovalError, _RemovalPlan
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

    def factory(handler=scripted_llm_handler, entity_types=None):
        update = {
            "data_dir": tmp_path / ".mb",
            "logs_dir": tmp_path / "logs",
            "wiki": WikiSettings(dir=tmp_path / "wiki", min_mentions=1),
            "chunking": ChunkingSettings(max_tokens=128),
        }
        if entity_types is not None:
            update["extract"] = ExtractSettings(entity_types=entity_types)
        settings = load_settings(config).model_copy(update=update)
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


async def test_renamed_file_moves_citations_to_the_new_path(make_app, docs):
    app = make_app()
    await app.pipeline.ingest([docs])
    old = docs / "attention_en.md"
    doc_id = file_doc_id(old)
    calls = len(app.llm.calls_for("extract"))
    new = docs / "renamed.md"
    old.rename(new)

    report = await app.pipeline.ingest([docs])
    assert report.failed == {} and report.processed == []
    assert str(new.resolve()) in report.skipped  # same content: nothing is redone
    assert len(app.llm.calls_for("extract")) == calls
    path = str(new.resolve())
    assert document_path(app, doc_id) == path
    assert chunk_paths(app, doc_id) == {path}
    doc, chunks = app.pipeline.load_chunks(doc_id)
    assert doc.path == path and {c.path for c in chunks} == {path}


async def test_renamed_then_edited_file_removes_the_old_version(make_app, tmp_path):
    folder = tmp_path / "renamed"
    folder.mkdir()
    a = folder / "a.md"
    a.write_text("# Notes\n\nSoftmax turns scores into probabilities.\n", encoding="utf-8")
    app = make_app()
    await app.pipeline.ingest([folder])
    old = file_doc_id(a)
    softmax = entity_id("Concept", "Softmax")
    page = tmp_path / "wiki" / "entities" / "softmax.md"
    assert page.exists()

    b = folder / "b.md"
    a.rename(b)  # the registry still lists a.md with the old content
    renamed = await app.pipeline.ingest([folder])
    assert renamed.skipped == [str(b.resolve())]
    b.write_text("# Notes\n\nThe Transformer is a network.\n", encoding="utf-8")
    report = await app.pipeline.ingest([folder])

    assert report.failed == {} and report.processed == [str(b.resolve())]
    assert not app.graph.has_document(old)  # a.md is gone: it does not keep the old version
    assert chunk_count(app, old) == 0
    assert app.graph.counts()["Document"] == 1
    assert app.graph.get_entity(softmax) is None
    assert _points(app, app.vectors.entities, "entity_id", softmax) == 0
    assert "Softmax" in report.wiki.deleted and not page.exists()
    assert app.registry.pending_removals() == []


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


async def test_embedder_template_change_is_refused(make_app, docs):
    app = make_app()
    await app.pipeline.ingest([docs], build_wiki=False)
    app.embedder.templates = ("passage: {text}", app.embedder.templates[1])
    with pytest.raises(EmbedderMismatchError, match="templates changed.*reindex"):
        await app.pipeline.ingest([docs], build_wiki=False)


async def test_legacy_signature_of_the_same_model_is_accepted_and_upgraded(make_app):
    app = make_app()
    app.registry.set_meta("embedder", "fake-embed:64")  # written before templates were signed
    app.pipeline.prepare()
    assert app.registry.get_meta("embedder") == embedder_signature(app.embedder)

    app.registry.set_meta("embedder", "fake-embed:32")  # legacy, but another dimension
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


def _points(app, collection: str, key: str, value: str) -> int:
    flt = qm.Filter(must=[qm.FieldCondition(key=key, match=qm.MatchValue(value=value))])
    return app.vectors.client.count(collection, count_filter=flt).count


def _fail_once(monkeypatch, target, name: str, after_call: bool = False) -> None:
    real = getattr(target, name)
    state = {"failed": False}

    def wrapper(*args, **kwargs):
        if state["failed"]:
            return real(*args, **kwargs)
        state["failed"] = True
        if after_call:
            real(*args, **kwargs)
        raise RuntimeError(f"{name} interrupted")

    monkeypatch.setattr(target, name, wrapper)


@pytest.mark.parametrize(
    ("where", "name", "after_call"),
    [
        ("vectors", "delete_entities", False),  # Qdrant fails mid-cleanup
        ("vectors", "delete_wiki_page", False),
        ("graph", "delete_document", True),  # the graph transaction committed, then a crash
        ("registry", "clear_doc", False),
    ],
)
async def test_interrupted_document_removal_is_finished_on_retry(
    make_app, tmp_path, monkeypatch, where, name, after_call
):
    folder = tmp_path / "linked"
    folder.mkdir()
    gone = folder / "gone.md"
    gone.write_text("# Notes\n\nSoftmax turns scores into probabilities.\n", encoding="utf-8")
    (folder / "kept.md").write_text("# Models\n\nThe Transformer is a network.\n", encoding="utf-8")
    app = make_app()
    await app.pipeline.ingest([folder])
    softmax = entity_id("Concept", "Softmax")
    softmax_page = tmp_path / "wiki" / "entities" / "softmax.md"
    transformer_page = tmp_path / "wiki" / "entities" / "transformer.md"
    assert softmax_page.exists() and _points(app, app.vectors.wiki, "page_id", softmax) > 0
    assert "[[softmax|Softmax]]" in transformer_page.read_text(encoding="utf-8")

    gone.write_text("# Notes\n\nNothing to see here.\n", encoding="utf-8")
    _fail_once(monkeypatch, getattr(app, where), name, after_call)
    broken = await app.pipeline.ingest([folder], build_wiki=False)
    assert list(broken.failed) == [str(gone.resolve())]

    report = await app.pipeline.ingest([folder])
    assert report.failed == {} and report.processed == [str(gone.resolve())]
    assert app.graph.get_entity(softmax) is None
    assert not softmax_page.exists()
    assert _points(app, app.vectors.wiki, "page_id", softmax) == 0  # no orphan wiki sections
    assert _points(app, app.vectors.entities, "entity_id", softmax) == 0
    assert "Softmax" in report.wiki.deleted
    # the page that linked to the removed one was marked dirty and rewritten
    assert "Transformer" in report.wiki.updated
    assert "[[softmax|" not in transformer_page.read_text(encoding="utf-8")
    assert app.registry.dirty() == []
    assert app.registry.pending_removals() == []


@pytest.mark.parametrize(
    ("where", "name", "after_call"),
    [
        ("vectors", "delete_entities", False),  # after the graph transaction committed
        ("graph", "delete_document", True),  # the graph transaction committed, then a crash
        ("graph", "delete_document", False),  # before the graph transaction
    ],
)
async def test_replayed_removal_spares_entities_revived_later_in_the_run(
    make_app, tmp_path, monkeypatch, where, name, after_call
):
    folder = tmp_path / "moved"
    folder.mkdir()
    gone = folder / "gone.md"
    kept = folder / "kept.md"
    paragraph = "Softmax turns scores into probabilities."
    gone.write_text(f"# Notes\n\n{paragraph}\n", encoding="utf-8")
    kept.write_text("# Models\n\nThe Transformer is a network.\n", encoding="utf-8")
    app = make_app()
    await app.pipeline.ingest([folder])
    softmax = entity_id("Concept", "Softmax")
    page = tmp_path / "wiki" / "entities" / "softmax.md"

    # one run: the paragraph moves from gone.md to kept.md, and gone.md's removal fails;
    # kept.md (processed later in the same run) mentions Softmax again
    gone.write_text("# Notes\n\nNothing to see here.\n", encoding="utf-8")
    kept.write_text(f"# Models\n\nThe Transformer is a network.\n\n{paragraph}\n", encoding="utf-8")
    _fail_once(monkeypatch, getattr(app, where), name, after_call)
    broken = await app.pipeline.ingest([folder])
    assert list(broken.failed) == [str(gone.resolve())]

    report = await app.pipeline.ingest([folder])
    assert report.failed == {}
    entity = app.graph.get_entity(softmax)
    assert entity is not None and entity.mention_count == 1
    assert _points(app, app.vectors.entities, "entity_id", softmax) == 1
    assert _points(app, app.vectors.wiki, "page_id", softmax) > 0
    assert page.exists() and app.graph.wiki_page(softmax) is not None
    assert "Softmax" not in report.wiki.deleted
    assert app.registry.dirty() == []
    assert app.registry.pending_removals() == []


async def test_stuck_pending_removal_names_the_document(make_app, tmp_path, monkeypatch):
    folder = tmp_path / "stuck"
    folder.mkdir()
    doc = folder / "a.md"
    doc.write_text("# Notes\n\nSoftmax turns scores into probabilities.\n", encoding="utf-8")
    app = make_app()
    await app.pipeline.ingest([folder], build_wiki=False)
    old = file_doc_id(doc)
    doc.write_text("# Notes\n\nNothing here.\n", encoding="utf-8")

    def down(*args, **kwargs):
        raise RuntimeError("qdrant down")

    monkeypatch.setattr(app.vectors, "delete_entities", down)
    first = await app.pipeline.ingest([folder], build_wiki=False)
    assert list(first.failed) == [str(doc.resolve())]
    with pytest.raises(PendingRemovalError, match=old) as info:
        await app.pipeline.ingest([folder], build_wiki=False)
    assert "qdrant down" in str(info.value) and "mnogobase doctor" in str(info.value)
    assert [d for d, _ in app.registry.pending_removals()] == [old]


@pytest.mark.parametrize("after_call", [False, True])  # before / after the graph commit
async def test_removal_spares_a_document_a_later_file_shares(
    make_app, tmp_path, monkeypatch, after_call
):
    folder = tmp_path / "shared"
    folder.mkdir()
    a = folder / "a.md"
    text = "# Notes\n\nSoftmax turns scores into probabilities.\n"
    a.write_text(text, encoding="utf-8")
    app = make_app()
    await app.pipeline.ingest([folder], build_wiki=False)
    x = file_doc_id(a)
    softmax = entity_id("Concept", "Softmax")
    # one run: a.md changes and its removal fails; a new b.md has a's old content
    b = folder / "b.md"
    b.write_text(text, encoding="utf-8")
    a.write_text("# Notes\n\nNothing here.\n", encoding="utf-8")
    _fail_once(monkeypatch, app.graph, "delete_document", after_call)
    broken = await app.pipeline.ingest([folder], build_wiki=False)
    assert list(broken.failed) == [str(a.resolve())]

    def b_is_live() -> None:
        assert app.graph.has_document(x)
        assert app.graph.get_entity(softmax) is not None
        assert _points(app, app.vectors.chunks, "doc_id", x) > 0
        assert _points(app, app.vectors.entities, "entity_id", softmax) == 1
        assert app.registry.pending_stages(x) == []
        assert app.registry.get_file(str(b.resolve())).status == "done"
        assert document_path(app, x) == str(b.resolve())  # citations name a live path

    b_is_live()
    app.pipeline.finish_pending_removals()  # what `wiki build` / the next ingest do first
    b_is_live()
    report = await app.pipeline.ingest([folder])
    assert report.failed == {}
    b_is_live()
    assert app.registry.pending_removals() == []
    assert (tmp_path / "wiki" / "entities" / "softmax.md").exists()


async def test_replay_keeps_a_page_file_another_entity_now_owns(make_app, tmp_path, monkeypatch):
    def handler(task, prompt):
        fragment = prompt.split("Fragment:", 1)[-1].casefold()
        if task == "extract" and "softmax layer" in fragment:
            layer = {"name": "Softmax", "type": "Method", "description": "A layer.", "aliases": []}
            return json.dumps({"entities": [layer], "relations": []})
        return scripted_llm_handler(task, prompt)

    folder = tmp_path / "slug"
    folder.mkdir()
    gone = folder / "gone.md"
    gone.write_text("# Notes\n\nSoftmax turns scores into probabilities.\n", encoding="utf-8")
    app = make_app(handler)
    await app.pipeline.ingest([folder])
    concept = entity_id("Concept", "Softmax")
    page = tmp_path / "wiki" / "entities" / "softmax.md"
    assert app.graph.wiki_page(concept).path == "entities/softmax.md"

    # one run: gone.md's removal commits and then fails; a new file brings a different
    # entity whose page takes over the now free slug "softmax"
    gone.write_text("# Notes\n\nNothing here.\n", encoding="utf-8")
    (folder / "layer.md").write_text("# Layers\n\nThe softmax layer.\n", encoding="utf-8")
    _fail_once(monkeypatch, app.graph, "delete_document", after_call=True)
    await app.pipeline.ingest([folder])
    method = entity_id("Method", "Softmax")
    assert app.graph.wiki_page(method).path == "entities/softmax.md"

    await app.pipeline.ingest([folder])  # replays gone.md's removal
    assert app.registry.pending_removals() == []
    assert page.exists() and f"id: {method}" in page.read_text(encoding="utf-8")
    assert _points(app, app.vectors.wiki, "page_id", method) > 0
    assert _points(app, app.vectors.wiki, "page_id", concept) == 0


async def test_failed_pending_removal_names_only_itself_and_keeps_finished_names(
    make_app, monkeypatch
):
    app = make_app()
    app.pipeline.prepare()
    done, stuck = "a" * 16, "b" * 16
    for doc_id, name in ((done, "Alpha"), (stuck, "Beta")):
        plan = _RemovalPlan(
            removed_entities={f"e-{name}": name}, removed_pages=[], dirty=[], relinked=0
        )
        app.registry.put_removal(doc_id, plan.model_dump_json())
    real = app.vectors.delete_doc

    def flaky(doc_id):
        if doc_id == stuck:
            raise RuntimeError("qdrant down")
        real(doc_id)

    monkeypatch.setattr(app.vectors, "delete_doc", flaky)
    with pytest.raises(PendingRemovalError) as info:
        app.pipeline.finish_pending_removals()
    assert stuck in str(info.value) and done not in str(info.value)
    assert [d for d, _ in app.registry.pending_removals()] == [stuck]

    monkeypatch.setattr(app.vectors, "delete_doc", real)
    assert app.pipeline.finish_pending_removals() == ["Alpha", "Beta"]  # Alpha not lost
    assert app.pipeline.finish_pending_removals() == []


async def test_changed_entity_types_warn_until_everything_is_reextracted(make_app, docs):
    app = make_app()
    report = await app.pipeline.ingest([docs], build_wiki=False)
    assert report.types_warning is None

    new_types = {"Person": "A human.", "Other": "Anything else."}
    changed = make_app(entity_types=new_types)
    with capture_logs() as logs:
        skipped = await changed.pipeline.ingest([docs], build_wiki=False)
    assert len(skipped.skipped) == 2  # unchanged documents keep their old types
    assert skipped.types_warning is not None and "mnogobase reset" in skipped.types_warning
    assert any(e["event"] == "entity_types_changed" for e in logs)

    # a new document is extracted with the new types; the old ones still have the old types
    (docs / "extra.md").write_text("# Extra\n\nSoftmax and attention again.\n", encoding="utf-8")
    mixed = await changed.pipeline.ingest([docs], build_wiki=False)
    assert len(mixed.processed) == 1 and mixed.types_warning is not None
    assert changed.graph.get_entity(entity_id("Other", "Softmax")) is not None

    # editing both old documents re-extracts them with the new types: no warning, no reset
    for name in ("attention_en.md", "vnimanie_ru.md"):
        path = docs / name
        path.write_text(path.read_text(encoding="utf-8") + "\nEdited.\n", encoding="utf-8")
    edited = await changed.pipeline.ingest([docs], build_wiki=False)
    assert len(edited.processed) == 2 and edited.types_warning is None
    assert changed.graph.get_entity(entity_id("Method", "Attention Mechanism")) is None
    assert changed.graph.get_entity(entity_id("Other", "Attention Mechanism")) is not None

    # reset + ingest is the other way to re-extract everything
    stale = make_app()  # back to the defaults: everything is stale again
    assert (await stale.pipeline.ingest([docs], build_wiki=False)).types_warning is not None
    reset(stale)
    fresh = await stale.pipeline.ingest([docs], build_wiki=False)
    assert len(fresh.processed) == 3 and fresh.types_warning is None
    assert stale.graph.get_entity(entity_id("Method", "Attention Mechanism")) is not None
