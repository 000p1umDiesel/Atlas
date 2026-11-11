"""KnowledgeBase + worker end to end on fake models: upload → ingest → answers → removal."""

import asyncio
import shutil
from pathlib import Path

import pytest
from qdrant_client import QdrantClient
from typer.testing import CliRunner

from mnogobase import cli
from mnogobase.app import build_app
from mnogobase.config import load_settings
from mnogobase.kb import KnowledgeBase
from mnogobase.service.errors import ServiceError
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

    monkeypatch.setattr(cli, "build_app", factory)
    monkeypatch.setattr(cli, "run_checks", lambda *a, **k: [])
    monkeypatch.setattr(cli, "configure_logging", lambda *a: None)
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
        job = kb.upload("Attention paper.md", content)
        assert job["status"] == "pending" and "path" not in job
        assert kb.upload("Attention paper.md", content)["id"] == job["id"]  # без дубликатов
        await work_once()

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
        history = kb.history(user["id"], chat["id"])
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
        ]:
            with pytest.raises(ServiceError) as error:
                kb.upload(name, body)
            assert error.value.status == status
        kb.upload("a.md", b"first")
        with pytest.raises(ServiceError) as error:
            kb.upload("b.md", b"second")
        assert error.value.status == 429
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
