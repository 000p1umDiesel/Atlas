"""CLI → настоящий HTTP → командный backend → реальные хранилища и API LLM."""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import threading
from pathlib import Path

import httpx
import pytest
import uvicorn

from mnogobase.api.app import create_api
from mnogobase.app import build_app
from mnogobase.config import load_settings
from mnogobase.embedding.openai_compat import OpenAICompatEmbedder
from mnogobase.service.runtime import TeamService
from mnogobase.stores.graph_store import GraphStore
from mnogobase.stores.qdrant_store import QdrantStore

pytestmark = pytest.mark.e2e
ROOT = Path(__file__).parents[2]


async def test_team_http_cli_live_models(neo4j_container, qdrant_client, tmp_path, pg_settings):
    settings = load_settings(ROOT / "config.yaml")
    settings.service.root = tmp_path / "service"
    settings.database = pg_settings.database
    settings.wiki.min_mentions = 1
    settings.llm.concurrency = 4
    settings.llm.timeout_s = 90
    assert settings.llm.serve == "api", "This scenario intentionally uses the configured LLM API"
    probe = OpenAICompatEmbedder(settings.embedder)
    try:
        dim = probe.dim
    finally:
        probe.close()

    def factory(scoped):
        return build_app(
            scoped,
            graph=GraphStore(neo4j_container.get_driver(), team_id=scoped.neo4j.team_id),
            vectors=QdrantStore(qdrant_client, scoped.qdrant.prefix, dim),
        )

    def graph_factory(options):
        return GraphStore(neo4j_container.get_driver(), team_id=options.team_id)

    service = TeamService(settings, app_factory=factory, graph_factory=graph_factory)
    user = service.store.create_user("live-alice", "test-only live password")
    teams = [service.store.create_team(name) for name in ("Live A", "Live B")]
    for team in teams:
        service.store.add_member(team["id"], user["id"], "admin")
    other = service.store.create_user("live-bob", "test-only other password")
    service.store.add_member(teams[1]["id"], other["id"])

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(create_api(service=service), log_level="error"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    credentials = tmp_path / "credentials.json"

    async def cli(team, *args):
        process = await asyncio.create_subprocess_exec(
            str(ROOT / ".venv/bin/mnogobase"),
            "client",
            "--url",
            url,
            "--credentials",
            str(credentials),
            "--team",
            team["id"],
            *args,
            cwd=ROOT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, error = await asyncio.wait_for(process.communicate(), timeout=360)
        assert process.returncode == 0, error.decode()[-2000:]
        return out.decode()

    try:
        async with httpx.AsyncClient(base_url=url, timeout=360) as http:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.1)
            assert server.started
            await cli(teams[0], "login", "live-alice", "--password", "test-only live password")
            assert len(json.loads(await cli(teams[0], "teams"))) == 2
            source = tmp_path / "paper.md"
            source.write_text(
                "# Transformer\n\n"
                "The Transformer is a neural network architecture proposed by Ashish Vaswani "
                "and coauthors in 2017. It replaces recurrent neural networks with self-attention.\n",
                encoding="utf-8",
            )
            doc_id = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
            for team in teams:
                queued = json.loads(await cli(team, "documents", "upload", str(source)))
                assert queued["status"] == "pending"
            assert await service.work_once() == 2
            for team in teams:
                docs = json.loads(await cli(team, "documents", "list"))
                assert docs[0]["id"] == doc_id and docs[0]["status"] == "done"
            # Real ingest usage is persisted independently for each team.
            for team in teams:
                usage = service.observability.usage(team_id=team["id"])
                assert usage and sum(row["tokens_in"] or 0 for row in usage) > 0
                assert all(row["team_id"] == team["id"] for row in usage)
            answer = json.loads(await cli(teams[0], "ask", "What is the Transformer?", "-m", "rag"))
            assert answer["sources"] and any(s["cited"] for s in answer["sources"]), answer
            assert str(settings.service.root) not in json.dumps(answer)
            chat = json.loads(await cli(teams[0], "chat", "new", "--title", "Live discussion"))
            first = json.loads(
                await cli(teams[0], "chat", "send", chat["id"], "Who proposed the Transformer?")
            )
            assert first["sources"] and first["status"] == "done"
            followup = json.loads(
                await cli(teams[0], "chat", "send", chat["id"], "What does it replace?")
            )
            assert followup["sources"] and followup["query"], followup
            history = json.loads(await cli(teams[0], "chat", "history", chat["id"]))
            assert len(history) == 4
            downloaded = tmp_path / "download.md"
            await cli(teams[0], "documents", "source", doc_id, "--output", str(downloaded))
            assert downloaded.read_bytes() == source.read_bytes()
            pages = json.loads(await cli(teams[0], "wiki", "list"))
            assert pages
            assert json.loads(await cli(teams[0], "wiki", "show", pages[0]["page_id"]))["content"]
            nodes = json.loads(await cli(teams[0], "graph", "search", "Transformer"))
            assert nodes
            assert json.loads(await cli(teams[0], "graph", "neighbors", nodes[0]["entity_id"]))[
                "nodes"
            ]

            bob_token = service.store.login("live-bob", "test-only other password")
            forbidden = await http.get(
                f"/api/teams/{teams[0]['id']}/documents/{doc_id}/content",
                headers={"Authorization": f"Bearer {bob_token}"},
            )
            assert forbidden.status_code == 403
            # Повторное создание runtime сохраняет очередь и историю, без смены корпуса.
            restarted = TeamService(settings, app_factory=factory, graph_factory=graph_factory)
            ctx = restarted.store.context(user["id"], teams[0]["id"])
            assert len(restarted.history(ctx, chat["id"])) == 4
            removal = json.loads(await cli(teams[0], "documents", "remove", doc_id))
            assert removal["kind"] == "remove"
            assert await restarted.work_once() == 1
            assert json.loads(await cli(teams[0], "documents", "list")) == []
            assert json.loads(await cli(teams[1], "search", "Transformer attention"))
            history = json.loads(await cli(teams[0], "chat", "history", chat["id"]))
            assert all(not s["available"] for m in history for s in m["sources"] if "doc_id" in s)
            service.store.remove_member(teams[0]["id"], user["id"])
            token = json.loads(credentials.read_text())[url]
            assert (
                await http.get(
                    f"/api/teams/{teams[0]['id']}/conversations",
                    headers={"Authorization": f"Bearer {token}"},
                )
            ).status_code == 403
            await cli(teams[1], "logout")
    finally:
        server.should_exit = True
        await asyncio.to_thread(thread.join, 10)
        sock.close()
        assert not thread.is_alive()
