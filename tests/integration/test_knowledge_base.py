"""KnowledgeBase + worker end to end on fake models: upload → ingest → answers → removal."""

import asyncio
import hashlib
import shutil
import tempfile
import time
from pathlib import Path

import pytest
from qdrant_client import QdrantClient
from typer.testing import CliRunner

from mnogobase import cli
from mnogobase.app import build_app
from mnogobase.core.config import load_settings
from mnogobase.ingest.pipeline import Pipeline
from mnogobase.llm.telemetry import CallEvent
from mnogobase.service.errors import ServiceError
from mnogobase.service.kb import KnowledgeBase
from mnogobase.stores.graph_store import DRIVER_OPTIONS, GraphStore
from mnogobase.stores.qdrant_store import QdrantStore
from tests.fakes import FakeEmbedder, FakeLLM, FakeSparse, scripted_llm_handler

pytestmark = [
    pytest.mark.integration,
    pytest.mark.filterwarnings(
        "ignore:Payload indexes have no effect in the local Qdrant:UserWarning"
    ),
]
FIXTURES = Path(__file__).parents[1] / "fixtures"
runner = CliRunner()


def upload(kb: KnowledgeBase, filename: str, content: bytes) -> dict:
    """Загрузка из памяти так же, как её делает API: временный файл, затем `upload_file`."""
    kb.upload_suffix(filename)
    with tempfile.NamedTemporaryFile(dir=kb.staging_dir(), prefix=".upload-", delete=False) as f:
        staged = Path(f.name)
        f.write(content)
    try:
        return kb.upload_file(filename, staged, hashlib.sha256(content).hexdigest())
    finally:
        staged.unlink(missing_ok=True)


def handler(task: str, prompt: str) -> str:
    if task == "query":
        return "What is the Transformer?"
    return scripted_llm_handler(task, prompt)


@pytest.fixture
def env(graph, neo4j_container, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(
        "wiki: {dir: wiki, min_mentions: 1}\n"
        "chunking: {tokenizer: Qwen/Qwen3-Embedding-4B, max_tokens: 128}\n"
        "retrieval: {translate: false}\n"
        "service: {uploads_dir: documents/uploads}\n",
        encoding="utf-8",
    )
    settings = load_settings(tmp_path / "config.yaml")
    shared = QdrantClient(":memory:")  # общий для App в API и в worker

    def factory(current):
        return build_app(
            current,
            embedder=FakeEmbedder(),
            sparse=FakeSparse(),
            llm=FakeLLM(handler),
            vectors=QdrantStore(shared, "t_", 64),
            graph=GraphStore(neo4j_container.get_driver(**DRIVER_OPTIONS)),
        )

    monkeypatch.setattr(cli.common, "build_app", factory)
    monkeypatch.setattr(cli.common, "run_checks", lambda *a, **k: [])
    monkeypatch.setattr(cli.common, "configure_logging", lambda *a: None)
    return settings, factory, tmp_path


async def work_once():
    # worker сам вызывает asyncio.run, поэтому не в цикле теста
    result = await asyncio.to_thread(runner.invoke, cli.app, ["worker", "run", "--once"])
    assert result.exit_code == 0, result.output


async def test_upload_ingest_answer_and_remove(env):
    settings, factory, root = env
    kb = KnowledgeBase(settings, app_factory=factory)
    user = kb.store.create_user("owner", "long-enough-password")
    try:
        content = (FIXTURES / "attention_en.md").read_bytes()
        job = upload(kb, "Attention paper.md", content)
        assert job["status"] == "pending" and "path" not in job and not job["duplicate"]
        same = upload(kb, "Attention paper.md", content)
        assert same["id"] == job["id"] and same["duplicate"]  # без дубликатов
        await work_once()
        indexed = upload(kb, "copy.md", content)  # то же содержимое под другим именем
        assert indexed["id"] == job["id"] and indexed["duplicate"]
        assert indexed["status"] == "done"

        [doc] = await kb.documents()
        assert doc["status"] == "done" and doc["uploaded"]
        assert doc["name"] == "Attention paper.md"
        data, name = kb.read_source(doc["id"])
        assert data == content and name == "Attention paper.md"

        answer = await kb.ask("What is the Transformer?", mode="rag")
        assert "[1]" in answer["text"]
        cited = [s for s in answer["sources"] if s.get("url") == doc["url"]]
        # источник назван так, как файл загрузили, а не внутренним `<doc_id>.md`
        assert cited and {s["path"] for s in cited} == {"Attention paper.md"}
        hit = (await kb.search("Transformer attention", k=3))[0]
        assert hit["doc_id"] == doc["id"] and hit["path"] == "Attention paper.md"
        assert await kb.ask("What is the Transformer?", mode="all")

        chat = kb.store.create_conversation(user["id"], "Chat")
        reply = await kb.send_message(user["id"], chat["id"], "What is the Transformer?")
        await kb.send_message(user["id"], chat["id"], "And its attention?")  # с историей
        assert reply["role"] == "assistant" and reply["sources"]
        assert "Attention paper.md" in {s.get("path") for s in reply["sources"]}
        history = await kb.history(user["id"], chat["id"])
        assert [m["role"] for m in history] == ["user", "assistant"] * 2
        assert any(s.get("available") for s in history[1]["sources"])

        pages = await kb.wiki_pages()
        assert pages and "path" not in pages[0]
        page = await kb.wiki_page(pages[0]["page_id"])
        assert page["content"]
        [entity, *_] = await kb.graph_search("Transformer")
        neighbors = await kb.graph_neighbors(entity["entity_id"])
        assert neighbors["nodes"]

        status = kb.status()
        assert status["worker"]["alive"] and status["queue"] == {"done": 1}

        removal = await kb.remove_document(doc["id"])
        assert removal["kind"] == "remove"
        await work_once()
        assert await kb.documents() == []
        assert not list((root / "documents/uploads").glob("*.md"))
        with pytest.raises(ServiceError):
            kb.read_source(doc["id"])
    finally:
        await kb.close()


async def test_removal_keeps_a_file_uploaded_again_meanwhile(env, monkeypatch):
    """Пока удаление документа выполняется, его загрузили снова: файл не должен пропасть."""
    settings, factory, root = env
    kb = KnowledgeBase(settings, app_factory=factory)
    try:
        content = (FIXTURES / "attention_en.md").read_bytes()
        upload(kb, "paper.md", content)
        await work_once()
        [doc] = await kb.documents()
        await kb.remove_document(doc["id"])

        original = Pipeline.remove

        async def remove_then_reupload(self, *args, **kwargs):
            report = await original(self, *args, **kwargs)
            upload(kb, "paper.md", content)  # новая загрузка во время работы remove-задачи
            return report

        monkeypatch.setattr(Pipeline, "remove", remove_then_reupload)
        await work_once()
        monkeypatch.setattr(Pipeline, "remove", original)
        assert list((root / "documents/uploads").glob("*.md"))  # файл новой загрузки на месте
        await work_once()
        assert [d["name"] for d in await kb.documents()] == ["paper.md"]
    finally:
        await kb.close()


async def test_documents_include_files_ingested_outside_the_api(env):
    settings, factory, root = env
    shutil.copy(FIXTURES / "vnimanie_ru.md", root / "local.md")
    result = await asyncio.to_thread(runner.invoke, cli.app, ["jobs", "enqueue", "local.md"])
    assert result.exit_code == 0, result.output
    await work_once()
    kb = KnowledgeBase(settings, app_factory=factory)
    try:
        [doc] = await kb.documents()
        assert doc == {
            "id": doc["id"],
            "name": "local.md",
            "status": "done",
            "uploaded": False,
            "url": None,
        }
        with pytest.raises(ServiceError):  # скачать можно только загруженное через API
            kb.read_source(doc["id"])
        await kb.remove_document(doc["id"])
        await work_once()
        assert await kb.documents() == []
        assert (root / "local.md").exists()  # чужой файл не удаляется, только забывается
    finally:
        await kb.close()


async def test_daily_token_limit_stops_questions_and_uploads(env):
    settings, factory, _ = env
    limited = settings.model_copy(
        update={"service": settings.service.model_copy(update={"daily_token_limit": 1000})}
    )
    kb = KnowledgeBase(limited, app_factory=factory)
    try:
        kb.check_budget()
        yesterday = time.time() - 2 * 86400
        for created, tokens in ((yesterday, 5000), (time.time(), 600), (time.time(), 500)):
            event = CallEvent(
                id=f"call-{created}-{tokens}",
                model="m",
                operation="answer",
                created_at=created,
                status="ok",
                tokens_in=tokens,
                tokens_out=0,
            )
            kb.usage.record(event)
        for call in (kb.ask("question"), kb.search("question")):
            with pytest.raises(ServiceError) as error:
                await call
            assert error.value.status == 429
        with pytest.raises(ServiceError) as error:
            upload(kb, "a.md", b"text")
        assert error.value.status == 429
        kb.check_budget(now=time.time() + 86400)  # следующие сутки — снова можно
    finally:
        await kb.close()


async def test_validation_and_queue_limits(env):
    settings, factory, _ = env
    limited = settings.model_copy(
        update={"service": settings.service.model_copy(update={"max_pending_jobs": 1})}
    )
    kb = KnowledgeBase(limited, app_factory=factory)
    try:
        for name, body, status in [
            ("../x.md", b"x", 422),
            ("x.exe", b"x", 422),
            ("x.md", b"", 422),
            ("x.pdf", b"<html>not a pdf</html>", 422),
            ("x.png", b"GIF89a", 422),
        ]:
            with pytest.raises(ServiceError) as error:
                upload(kb, name, body)
            assert error.value.status == status
        upload(kb, "a.md", b"first")
        with pytest.raises(ServiceError) as error:
            upload(kb, "b.md", b"second")
        assert error.value.status == 429
        assert not list(kb.staging_dir().glob(".upload-*"))
        with pytest.raises(ServiceError) as error:
            await kb.ask("   ")
        assert error.value.status == 422
        with pytest.raises(ServiceError) as error:
            await kb.ask("question", mode="vector")
        assert error.value.status == 422
        assert (await kb.ask("anything"))["sources"] == []  # пустой корпус
        with pytest.raises(ServiceError) as error:
            await kb.remove_document("0" * 16)
        assert error.value.status == 404
    finally:
        await kb.close()


async def test_messages_keep_metrics_and_first_question_titles_the_chat(env):
    settings, factory, root = env
    kb = KnowledgeBase(settings, app_factory=factory)
    user = kb.store.create_user("owner", "long-enough-password")
    try:
        upload(kb, "Attention paper.md", (FIXTURES / "attention_en.md").read_bytes())
        await work_once()
        chat = kb.store.create_conversation(user["id"])
        question = "What is the Transformer architecture and why does it use attention?" * 2
        reply = await kb.send_message(user["id"], chat["id"], question)
        assert isinstance(reply["meta"]["latency_ms"], int)
        assert reply["meta"]["tokens_out"] > 0
        assert kb.store.conversation(user["id"], chat["id"])["title"] == question[:60]
    finally:
        await kb.close()


async def test_document_card_chunks_and_named_jobs(env):
    settings, factory, root = env
    kb = KnowledgeBase(settings, app_factory=factory)
    try:
        job = upload(kb, "Attention paper.md", (FIXTURES / "attention_en.md").read_bytes())
        [queued] = await kb.documents()
        assert queued["status"] == "queued" and queued["name"] == "Attention paper.md"
        [listed] = kb.jobs()
        assert listed["id"] == job["id"] and listed["name"] == "Attention paper.md"
        assert listed["doc_id"] == queued["id"] and "path" not in listed
        early = await kb.document(queued["id"])
        assert early["status"] == "queued" and early["chunks"] == 0 and early["stages"] == []
        await work_once()

        [doc] = await kb.documents()
        card = await kb.document(doc["id"])
        assert card["name"] == "Attention paper.md" and card["status"] == "done"
        assert [s["status"] for s in card["stages"]] == ["done"] * 5
        assert card["chunks"] > 0 and card["entities"]
        assert all(isinstance(e["has_page"], bool) for e in card["entities"])
        assert {"entity_id", "name", "type"} <= set(card["entities"][0])

        page = await kb.document_chunks(doc["id"], limit=2, offset=1)
        assert page["total"] == card["chunks"] and page["document"]["name"] == "Attention paper.md"
        assert [c["idx"] for c in page["chunks"]] == [1, 2]
        first = await kb.chunk(f"{doc['id']}:00000")
        assert first["name"] == "Attention paper.md" and first["text"]
        assert first["url"] == doc["url"] and "path" not in first

        for bad in ("nope", f"{doc['id']}:99999"):
            with pytest.raises(ServiceError) as missing:
                await kb.chunk(bad)
            assert missing.value.status == 404
        with pytest.raises(ServiceError) as unknown:
            await kb.document("0" * 16)
        assert unknown.value.status == 404

        await kb.remove_document(doc["id"])
        await work_once()
        with pytest.raises(ServiceError) as removed:
            await kb.chunk(f"{doc['id']}:00000")
        assert removed.value.status == 404
    finally:
        await kb.close()


async def test_wiki_search_slugs_sources_and_entity_card(env):
    settings, factory, root = env
    kb = KnowledgeBase(settings, app_factory=factory)
    try:
        upload(kb, "Attention paper.md", (FIXTURES / "attention_en.md").read_bytes())
        await work_once()
        pages = await kb.wiki_pages(limit=200)
        assert pages
        target = pages[0]
        needle = target["title"][:4].upper()
        found = await kb.wiki_pages(q=needle, limit=200)
        assert target["page_id"] in {p["page_id"] for p in found}
        assert all(
            needle.lower() in (p["title"] + p["slug"] + " ".join(p["aliases"])).lower()
            for p in found
        )
        assert await kb.wiki_pages(q="no-such-page-zzz") == []

        page = await kb.wiki_page(target["page_id"])
        by_slug = await kb.wiki_page_by_slug(target["slug"])
        assert by_slug["id"] == page["id"] and by_slug["content"] == page["content"]
        cited = [p for p in [await kb.wiki_page(x["page_id"]) for x in pages] if p["sources"]]
        assert cited and set(cited[0]["sources"].values()) == {"Attention paper.md"}
        with pytest.raises(ServiceError) as missing:
            await kb.wiki_page_by_slug("no-such-slug")
        assert missing.value.status == 404

        entity = await kb.entity(target["page_id"])
        assert entity["has_page"] is True and entity["slug"] == target["slug"]
        assert entity["pagerank"] is None and entity["name"]
        with pytest.raises(ServiceError):
            await kb.entity("0" * 16)
        analysis = await kb.graph_analysis()
        assert analysis["ready"] is False and analysis["top"] == []
    finally:
        await kb.close()


async def _ready_kb(env):
    settings, factory, root = env
    kb = KnowledgeBase(settings, app_factory=factory)
    user = kb.store.create_user("owner", "long-enough-password")
    upload(kb, "Attention paper.md", (FIXTURES / "attention_en.md").read_bytes())
    await work_once()
    return kb, user, kb.store.create_conversation(user["id"])


async def test_stream_message_events_and_history(env):
    kb, user, chat = await _ready_kb(env)
    try:
        events = await kb.send_message_stream(user["id"], chat["id"], "What is the Transformer?")
        seen = [e async for e in events]
        names = [e["event"] for e in seen]
        assert names[0] == "sources" and names[-1] == "done" and "delta" in names
        assert set(names[1:-1]) == {"delta"}
        assert seen[0]["data"] and "doc_id" in seen[0]["data"][0]
        text = "".join(e["data"]["text"] for e in seen if e["event"] == "delta")
        done = seen[-1]["data"]
        assert done["content"].strip() == text.strip() and done["role"] == "assistant"
        assert any(s["cited"] for s in done["sources"]) and done["meta"]["latency_ms"] >= 0
        history = await kb.history(user["id"], chat["id"])
        assert [(m["role"], m["status"]) for m in history] == [
            ("user", "done"),
            ("assistant", "done"),
        ]
        assert kb.store.conversation(user["id"], chat["id"])["title"]
    finally:
        await kb.close()


async def test_stream_answer_is_saved_after_the_client_leaves(env):
    kb, user, chat = await _ready_kb(env)
    try:
        events = await kb.send_message_stream(user["id"], chat["id"], "What is the Transformer?")
        first = await anext(events)
        assert first["event"] == "sources"
        await events.aclose()
        for _ in range(50):
            history = await kb.history(user["id"], chat["id"])
            if len(history) == 2:
                break
            await asyncio.sleep(0.1)
        assert [(m["role"], m["status"]) for m in history][-1] == ("assistant", "done")
    finally:
        await kb.close()


async def test_close_waits_for_a_running_stream_answer(env, monkeypatch):
    kb, user, chat = await _ready_kb(env)
    app = await kb._core.run(kb._ensure_app())
    original = app.llm.stream

    async def slow(*args, **kwargs):
        await asyncio.sleep(0.3)
        async for piece in original(*args, **kwargs):
            yield piece

    monkeypatch.setattr(app.llm, "stream", slow)
    events = await kb.send_message_stream(user["id"], chat["id"], "What is the Transformer?")
    await events.aclose()
    await kb.close()
    last = kb.store.messages(user["id"], chat["id"])[-1]
    assert (last["role"], last["status"]) == ("assistant", "done")


async def test_close_marks_an_unfinished_stream_answer_as_failed(env, monkeypatch):
    from mnogobase.service import kb as kb_module

    monkeypatch.setattr(kb_module, "SHUTDOWN_GRACE_S", 0.2)
    kb, user, chat = await _ready_kb(env)
    app = await kb._core.run(kb._ensure_app())

    async def stuck(*args, **kwargs):
        await asyncio.sleep(60)
        yield ""

    monkeypatch.setattr(app.llm, "stream", stuck)
    events = await kb.send_message_stream(user["id"], chat["id"], "What is the Transformer?")
    await asyncio.wait_for(kb.close(), 10)
    seen = [e async for e in events]
    assert seen[-1]["event"] == "error" and seen[-1]["data"]["status"] == 503
    last = kb.store.messages(user["id"], chat["id"])[-1]
    assert (last["role"], last["status"]) == ("assistant", "error")


async def test_stream_reports_llm_failure(env, monkeypatch):
    kb, user, chat = await _ready_kb(env)
    try:

        async def broken(*args, **kwargs):
            raise RuntimeError("provider says http://secret")
            yield ""

        app = await kb._core.run(kb._ensure_app())
        monkeypatch.setattr(app.llm, "stream", broken)
        events = await kb.send_message_stream(user["id"], chat["id"], "What is the Transformer?")
        seen = [e async for e in events]
        assert seen[-1]["event"] == "error" and seen[-1]["data"]["status"] == 503
        assert "secret" not in str(seen)
        assert (await kb.history(user["id"], chat["id"]))[-1]["status"] == "error"
        with pytest.raises(ServiceError) as missing:
            await kb.send_message_stream(user["id"], "nope", "Q?")
        assert missing.value.status == 404
    finally:
        await kb.close()


def _job(kb, job_id):
    return next(j for j in kb.jobs(limit=200) if j["id"] == job_id)


async def test_analyze_job_dedups_and_runs_in_the_worker(env, monkeypatch):
    from mnogobase.cli import jobs as jobs_cli
    from mnogobase.ops.doctor import Check

    settings, factory, root = env
    calls = []
    monkeypatch.setattr(jobs_cli, "analyze", lambda graph, cfg: calls.append(cfg) or {})
    # для анализа нужен только Neo4j: недоступные модели задачу не откладывают
    monkeypatch.setattr(
        cli.common,
        "run_checks",
        lambda settings, only=None, **kw: [Check(n, n == "neo4j", "x") for n in only or ()],
    )
    kb = KnowledgeBase(settings, app_factory=factory)
    try:
        job = await asyncio.to_thread(kb.enqueue_analyze)
        assert job["kind"] == "analyze" and job["name"] == "Анализ графа"
        assert (await asyncio.to_thread(kb.enqueue_analyze))["id"] == job["id"]
        await work_once()
        assert _job(kb, job["id"])["status"] == "done" and len(calls) == 1
        again = await asyncio.to_thread(kb.enqueue_analyze)
        assert again["id"] == job["id"] and again["status"] == "pending"
        # задача видна в состоянии анализа, сколько бы новых задач ни было в очереди
        assert (await kb.graph_analysis())["job"]["id"] == job["id"]
    finally:
        await kb.close()


async def test_graph_analysis_without_a_job(env):
    settings, factory, root = env
    kb = KnowledgeBase(settings, app_factory=factory)
    try:
        assert (await kb.graph_analysis())["job"] is None
    finally:
        await kb.close()


async def test_analyze_without_gds_fails_once_with_its_message(env):
    settings, factory, root = env
    kb = KnowledgeBase(settings, app_factory=factory)
    try:
        job = await asyncio.to_thread(kb.enqueue_analyze)
        # --once выходит с кодом 1, когда в очереди остались упавшие задачи
        await asyncio.to_thread(runner.invoke, cli.app, ["worker", "run", "--once"])
        done = _job(kb, job["id"])
        assert done["status"] == "failed" and done["attempts"] == 1
        assert done["error"].startswith("AnalysisError: ") and "GDS" in done["error"]
    finally:
        await kb.close()


async def test_analyze_waits_while_another_analysis_runs(env, monkeypatch):
    from mnogobase.cli import jobs as jobs_cli
    from mnogobase.core.database import database_lock

    settings, factory, root = env
    monkeypatch.setattr(jobs_cli, "analyze", lambda graph, cfg: {})
    kb = KnowledgeBase(settings, app_factory=factory)
    try:
        job = await asyncio.to_thread(kb.enqueue_analyze)
        with database_lock(settings, "graph-analysis"):
            await asyncio.to_thread(runner.invoke, cli.app, ["worker", "run", "--once"])
        waiting = _job(kb, job["id"])
        assert waiting["status"] == "pending" and waiting["attempts"] == 0
    finally:
        await kb.close()


async def test_unexpected_job_errors_keep_only_the_type(env, monkeypatch):
    from mnogobase.cli import jobs as jobs_cli

    def broken(graph, cfg):
        raise RuntimeError("bolt://user:secret@host")

    settings, factory, root = env
    monkeypatch.setattr(jobs_cli, "analyze", broken)
    kb = KnowledgeBase(settings, app_factory=factory)
    try:
        job = await asyncio.to_thread(kb.enqueue_analyze)
        await asyncio.to_thread(runner.invoke, cli.app, ["worker", "run", "--once"])
        assert _job(kb, job["id"])["error"] == "RuntimeError"
    finally:
        await kb.close()


async def test_stream_always_ends_with_a_terminal_event(env, monkeypatch):
    kb, user, chat = await _ready_kb(env)
    try:

        async def broken(*args, **kwargs):
            raise RuntimeError("provider down")
            yield ""

        app = await kb._core.run(kb._ensure_app())
        monkeypatch.setattr(app.llm, "stream", broken)
        events = await kb.send_message_stream(user["id"], chat["id"], "What is the Transformer?")

        def database_down(*args, **kwargs):
            raise RuntimeError("database down")

        # вопрос уже сохранён; дальше база «падает» — и для ответа, и для записи ошибки
        monkeypatch.setattr(kb.store, "append_message", database_down)
        seen = await asyncio.wait_for(_collect(events), timeout=10)
        assert seen[-1]["event"] == "error" and seen[-1]["data"]["status"] == 503
    finally:
        await kb.close()


async def _collect(events):
    return [e async for e in events]


async def test_history_marks_cli_ingested_sources_available_until_removed(env):
    settings, factory, root = env
    shutil.copy(FIXTURES / "attention_en.md", root / "local.md")
    result = await asyncio.to_thread(runner.invoke, cli.app, ["jobs", "enqueue", "local.md"])
    assert result.exit_code == 0, result.output
    await work_once()
    kb = KnowledgeBase(settings, app_factory=factory)
    user = kb.store.create_user("owner", "long-enough-password")
    try:
        chat = kb.store.create_conversation(user["id"])
        await kb.send_message(user["id"], chat["id"], "What is the Transformer?")
        await kb.send_message(user["id"], chat["id"], "And its attention?")
        history = await kb.history(user["id"], chat["id"])
        chunks = [s for s in history[1]["sources"] if s.get("kind") == "chunk"]
        assert chunks and all(s["available"] for s in chunks)
        newest = await kb.history(user["id"], chat["id"], limit=2, newest=True)
        assert [m["content"] for m in newest] == [history[2]["content"], history[3]["content"]]

        [doc] = await kb.documents()
        await kb.remove_document(doc["id"])
        await work_once()
        history = await kb.history(user["id"], chat["id"])
        chunks = [s for s in history[1]["sources"] if s.get("kind") == "chunk"]
        assert chunks and not any(s["available"] for s in chunks)
    finally:
        await kb.close()
