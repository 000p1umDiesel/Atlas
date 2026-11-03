"""Настоящие Qdrant/Neo4j; детерминированные модели для проверки сервиса команд."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from mnogobase.app import build_app
from mnogobase.background.queue import JobQueue
from mnogobase.config import Settings
from mnogobase.database import database_target
from mnogobase.ids import entity_id
from mnogobase.service.context import team_settings
from mnogobase.service.errors import ServiceError
from mnogobase.service.runtime import TeamService
from mnogobase.stores.graph_store import GraphStore
from mnogobase.stores.qdrant_store import QdrantStore
from tests.fakes import FakeEmbedder, FakeLLM, FakeSparse, scripted_llm_handler

pytestmark = pytest.mark.integration
FIXTURES = Path(__file__).parents[1] / "fixtures"


@pytest.fixture
def team_service(tmp_path, neo4j_container, qdrant_client, pg_settings):
    settings = Settings(
        device="cpu",
        service={"root": tmp_path / "service"},
        chunking={"tokenizer": "Qwen/Qwen3-Embedding-4B", "max_tokens": 128},
        retrieval={"translate": False},
        rerank={"enabled": False},
        vision={"enabled": False},
        wiki={"min_mentions": 1},
    )
    settings.database = pg_settings.database
    operations = []
    llms = []

    def handler(task, prompt):
        if task == "query":
            return "How does the Transformer use attention?"
        return scripted_llm_handler(task, prompt)

    def factory(scoped):
        operations.append(scoped.neo4j.team_id)
        llm = FakeLLM(handler)
        llms.append(llm)
        return build_app(
            scoped,
            embedder=FakeEmbedder(),
            sparse=FakeSparse(),
            llm=llm,
            vectors=QdrantStore(qdrant_client, scoped.qdrant.prefix, 64),
            graph=GraphStore(neo4j_container.get_driver(), team_id=scoped.neo4j.team_id),
        )

    service = TeamService(settings, app_factory=factory)
    user = service.store.create_user("owner", "integration password")
    teams = [service.store.create_team(name) for name in ("A", "B")]
    for team in teams:
        service.store.add_member(team["id"], user["id"], "admin")
    contexts = [service.store.context(user["id"], team["id"]) for team in teams]

    @contextmanager
    def graph_for(ctx):
        graph = GraphStore(neo4j_container.get_driver(), team_id=ctx.team_id)
        try:
            yield graph
        finally:
            graph.close()

    return SimpleNamespace(
        service=service,
        settings=settings,
        factory=factory,
        contexts=contexts,
        operations=operations,
        llms=llms,
        graph_for=graph_for,
        qdrant=qdrant_client,
    )


async def test_team_service_ingestion_retrieval_private_chat_and_deletion(team_service):
    env = team_service
    a, b = env.contexts
    service = env.service
    content = (FIXTURES / "attention_en.md").read_bytes()
    doc_id = hashlib.sha256(content).hexdigest()[:16]
    jobs = [service.upload(ctx, "paper.md", content) for ctx in (a, b)]
    assert all(job["status"] == "pending" for job in jobs)
    assert all("path" not in job for job in jobs)
    assert service.upload(a, "renamed.md", content)["id"] == jobs[0]["id"]
    assert len(service.jobs(a)) == len(service.jobs(b)) == 1
    assert service.documents(a)[0]["id"] == service.documents(b)[0]["id"] == doc_id
    assert service.source(a, doc_id) != service.source(b, doc_id)

    # Очереди и загруженные исходники переживают пересоздание процесса сервиса.
    service = TeamService(env.settings, app_factory=env.factory)
    assert await service.work_once() == 2
    for ctx in (a, b):
        assert [job["status"] for job in service.jobs(ctx)] == ["done"]
        assert service.documents(ctx)[0]["status"] == "done"

    transformer_id = entity_id("Method", "Transformer")
    chunk_ids = []
    for ctx in (a, b):
        with env.graph_for(ctx) as graph:
            assert graph.counts()["Document"] == 1
            assert graph.get_entity(transformer_id).name == "Transformer"
        scoped = team_settings(env.settings, ctx.team_id)
        points, _ = env.qdrant.scroll(scoped.qdrant.prefix + "chunks", limit=1000)
        ids = {point.payload["chunk_id"] for point in points}
        assert ids and all(value.startswith(doc_id + ":") for value in ids)
        chunk_ids.append(ids)
    assert chunk_ids[0] == chunk_ids[1]

    for mode in ("rag", "wiki", "graph", "all"):
        hits = await service.search(a, "Transformer attention", mode=mode)
        assert hits, mode
        assert all(str(env.settings.service.root) not in str(hit) for hit in hits)
    pages = service.wiki_pages(a)
    assert any(page["page_id"] == transformer_id for page in pages)
    assert service.wiki_page(a, transformer_id)["content"]
    assert any(row["entity_id"] == transformer_id for row in service.graph_search(a, "Transformer"))
    assert service.graph_neighbors(a, transformer_id)["nodes"]

    chat = service.store.create_conversation(a, "Transformer details")
    first = await service.send_message(a, chat["id"], "Explain Transformer attention")
    assert first["status"] == "done" and first["sources"]
    second = await service.send_message(a, chat["id"], "How does it work?")
    assert second["query"] == "How does the Transformer use attention?"
    rewrite_prompts = [prompt for llm in env.llms for prompt in llm.calls_for("query")]
    assert len(rewrite_prompts) == 1
    assert "Explain Transformer attention" in rewrite_prompts[0]
    history = service.history(a, chat["id"])
    assert [message["role"] for message in history] == ["user", "assistant", "user", "assistant"]
    assert all(source["available"] for message in history for source in message["sources"])
    assert service.history(a, chat["id"], limit=2, offset=2) == history[2:]

    # Тот же автор в соседней команде не получает личный диалог команды A.
    with pytest.raises(ServiceError) as error:
        service.history(b, chat["id"])
    assert error.value.status == 404
    stranger = service.store.create_user("other-admin", "integration password")
    service.store.add_member(a.team_id, stranger["id"], "admin")
    stranger_ctx = service.store.context(stranger["id"], a.team_id)
    with pytest.raises(ServiceError) as error:
        await service.send_message(stranger_ctx, chat["id"], "Read someone else's chat")
    assert error.value.status == 404

    removal = await service.remove_document(a, doc_id)
    assert removal["kind"] == "remove" and removal["status"] == "pending"
    assert len(service.jobs(a)) == 2 and len(service.jobs(b)) == 1
    assert await service.work_once() == 1
    assert service.documents(a) == []
    assert await service.search(a, "Transformer") == []
    with pytest.raises(ServiceError) as error:
        service.source(a, doc_id)
    assert error.value.status == 404
    for ctx, expected in ((a, 0), (b, 1)):
        with env.graph_for(ctx) as graph:
            assert graph.counts()["Document"] == expected
            assert (graph.get_entity(transformer_id) is not None) == bool(expected)
    assert service.source(b, doc_id).read_bytes() == content
    assert await service.search(b, "Transformer")
    after = service.history(a, chat["id"])
    assert [message["content"] for message in after] == [message["content"] for message in history]
    assert all(source["available"] is False for message in after for source in message["sources"])

    # Отозванный контекст нельзя использовать даже для уже готовых задач и истории.
    service.store.remove_member(a.team_id, a.user_id)
    for operation in (lambda: service.jobs(a), lambda: service.history(a, chat["id"])):
        with pytest.raises(ServiceError) as error:
            operation()
        assert error.value.status == 403
    with pytest.raises(ServiceError) as error:
        await service.ask(a, "Transformer")
    assert error.value.status == 403


async def test_worker_recovers_running_jobs_and_rotates_teams(team_service):
    env = team_service
    a, b = env.contexts
    for ctx in (a, b):
        for index in range(2):
            env.service.upload(
                ctx,
                f"paper-{index}.md",
                f"# Transformer {index}\n\nTransformer uses attention and softmax.\n".encode(),
            )
    path = database_target(team_settings(env.settings, a.team_id), "jobs")
    queue = JobQueue(path)
    try:
        interrupted = queue.claim()
        assert interrupted.status == "running"
    finally:
        queue.close()

    service = TeamService(env.settings, app_factory=env.factory)
    env.operations.clear()
    assert await service.work_once() == 2
    assert env.operations == [a.team_id, b.team_id]
    a_jobs = service.jobs(a)
    assert next(job for job in a_jobs if job["id"] == interrupted.id)["attempts"] == 2
    for ctx in (a, b):
        assert sorted(job["status"] for job in service.jobs(ctx)) == ["done", "pending"]

    env.operations.clear()
    assert await service.work_once() == 2
    assert env.operations == [b.team_id, a.team_id]
    for ctx in (a, b):
        assert all(job["status"] == "done" for job in service.jobs(ctx))
        assert len(service.documents(ctx)) == 2
        with env.graph_for(ctx) as graph:
            assert graph.counts()["Document"] == 2
    assert await service.work_once() == 0
