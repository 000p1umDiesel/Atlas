from __future__ import annotations

import importlib.util
from types import SimpleNamespace

import pytest


def test_api_factory_exists():
    assert importlib.util.find_spec("mnogobase.api.app") is not None


@pytest.fixture
def setup_api(tmp_path):
    from fastapi.testclient import TestClient

    from mnogobase.api.app import create_api
    from mnogobase.config import Settings
    from mnogobase.service.store import ServiceStore

    settings = Settings(service={"root": tmp_path, "max_upload_mb": 1})
    store = ServiceStore(tmp_path / "service.db")
    admin = store.create_user("admin", "long-enough-password")
    member = store.create_user("member", "long-enough-password")
    outsider = store.create_user("outsider", "long-enough-password")
    team = store.create_team("Alpha")
    store.add_member(team["id"], admin["id"], "admin")
    store.add_member(team["id"], member["id"])
    service = SimpleNamespace(store=store)
    client = TestClient(create_api(settings, service=service), raise_server_exceptions=False)
    token = store.login("admin", "long-enough-password")
    headers = {"Authorization": f"Bearer {token}"}
    return SimpleNamespace(**locals())


def test_auth_login_logout_and_redacted_me(setup_api):
    s = setup_api
    assert s.client.get("/api/auth/me").status_code == 401
    assert s.client.get("/api/auth/me", headers={"Cookie": f"token={s.token}"}).status_code == 401
    bad = s.client.post("/api/auth/login", json={"username": "admin", "password": "bad"})
    assert bad.status_code == 401
    response = s.client.post(
        "/api/auth/login", json={"username": "admin", "password": "long-enough-password"}
    )
    assert response.status_code == 200
    assert response.json()["token"]
    me = s.client.get("/api/auth/me", headers=s.headers).json()
    assert me["username"] == "admin"
    assert "password" not in str(me)
    assert s.client.post("/api/auth/logout", headers=s.headers).status_code == 200
    assert s.client.get("/api/auth/me", headers=s.headers).status_code == 401


def test_team_access_roles_ids_pagination_and_revocation(setup_api):
    s = setup_api
    prefix = f"/api/teams/{s.team['id']}"
    s.service.documents = lambda ctx, limit=50, offset=0: []
    assert len(s.client.get("/api/teams", headers=s.headers).json()) == 1
    assert s.client.get(prefix + "/documents", headers=s.headers).status_code == 200
    assert s.client.get(prefix + "/documents?limit=0", headers=s.headers).status_code == 422
    assert s.client.get(prefix + "/documents?offset=-1", headers=s.headers).status_code == 422
    assert (
        s.client.get("/api/teams/not-a-server-id/documents", headers=s.headers).status_code == 422
    )
    member_headers = {"Authorization": "Bearer " + s.store.login("member", "long-enough-password")}
    assert s.client.delete(prefix + "/documents/abc", headers=member_headers).status_code == 403
    assert (
        s.client.post(
            prefix + "/members", headers=member_headers, json={"user_id": s.outsider["id"]}
        ).status_code
        == 403
    )
    s.store.remove_member(s.team["id"], s.admin["id"])
    assert s.client.get(prefix + "/documents", headers=s.headers).status_code == 403


def test_upload_stream_limits_and_authorization(setup_api):
    s = setup_api
    prefix = f"/api/teams/{s.team['id']}/documents"
    s.service.upload = lambda ctx, filename, content: {"filename": filename, "size": len(content)}
    response = s.client.post(prefix + "?filename=report.txt", headers=s.headers, content=b"hello")
    assert response.status_code == 202
    assert response.json()["size"] == 5
    big = s.client.post(
        prefix + "?filename=report.txt",
        headers=s.headers,
        content=iter([b"a" * 600000, b"a" * 600000]),
    )
    assert big.status_code == 413
    assert s.client.post(prefix + "?filename=report.txt", content=b"x").status_code == 401


def test_async_result_rechecks_session_and_membership(setup_api):
    s = setup_api
    prefix = f"/api/teams/{s.team['id']}"

    async def ask(ctx, question, mode="rag"):
        s.store.logout(s.token)
        return {"text": "private answer"}

    s.service.ask = ask
    response = s.client.post(prefix + "/ask", headers=s.headers, json={"question": "question"})
    assert response.status_code == 401
    assert "private answer" not in response.text


def test_errors_are_safe_and_cors_not_permissive(setup_api):
    from mnogobase.service.errors import ServiceError

    s = setup_api
    prefix = f"/api/teams/{s.team['id']}"

    async def ask(ctx, question, mode="rag"):
        raise RuntimeError("/secret/file token=abc")

    s.service.ask = ask
    response = s.client.post(prefix + "/ask", headers=s.headers, json={"question": "question"})
    assert response.status_code == 503
    assert "secret" not in response.text and "abc" not in response.text
    assert "access-control-allow-origin" not in response.headers
    for status in (404, 409, 503):

        def fail(ctx, limit=50, offset=0, status=status):
            raise ServiceError(status, "Safe error")

        s.service.documents = fail
        assert s.client.get(prefix + "/documents", headers=s.headers).status_code == status


def test_private_conversations_persist_and_deny_other_author(setup_api):
    s = setup_api
    prefix = f"/api/teams/{s.team['id']}/conversations"
    created = s.client.post(prefix, headers=s.headers, json={"title": "My chat"})
    assert created.status_code == 201
    cid = created.json()["id"]
    member_headers = {"Authorization": "Bearer " + s.store.login("member", "long-enough-password")}
    assert s.client.get(prefix + f"/{cid}/messages", headers=member_headers).status_code in (
        403,
        404,
    )
    s.service.history = lambda ctx, conversation_id, limit=100, offset=0: s.store.messages(
        ctx, conversation_id, limit=limit, offset=offset
    )
    assert s.client.get(prefix + f"/{cid}/messages", headers=s.headers).json() == []


def test_all_route_groups_and_citations(setup_api):
    s = setup_api
    prefix = f"/api/teams/{s.team['id']}"
    source_file = s.tmp_path / "report.txt"
    source_file.write_text("source evidence")
    s.service.read_source = lambda ctx, doc_id: (source_file.read_bytes(), source_file.name)
    s.service.jobs = lambda ctx, limit=50, offset=0: [{"id": "job"}]
    s.service.retry_job = lambda ctx, job_id: {"id": job_id, "status": "queued"}
    s.service.wiki_pages = lambda ctx, limit=50, offset=0: [{"id": "page"}]
    s.service.wiki_page = lambda ctx, page_id: {"id": page_id, "text": "Wiki evidence"}
    s.service.graph_search = lambda ctx, query, limit=20: [{"id": "entity"}]
    s.service.graph_neighbors = lambda ctx, entity_id, depth=1, limit=50: {
        "id": entity_id,
        "edges": [],
    }

    async def search(ctx, question, mode="rag", k=8):
        return [{"text": "evidence", "source_url": prefix + "/documents/doc/content"}]

    async def remove(ctx, doc_id):
        return {"id": "delete-job", "status": "queued"}

    async def send(ctx, conversation_id, question, mode="rag"):
        s.store.append_message(ctx, conversation_id, "user", question, mode=mode)
        return s.store.append_message(
            ctx,
            conversation_id,
            "assistant",
            "Answer",
            mode=mode,
            sources=[{"doc_id": "doc", "source_url": prefix + "/documents/doc/content"}],
        )

    s.service.search = search
    s.service.remove_document = remove
    s.service.send_message = send
    s.service.history = lambda ctx, conversation_id, limit=100, offset=0: s.store.messages(
        ctx, conversation_id, limit=limit, offset=offset
    )
    assert s.client.get("/health").json() == {"status": "ok"}
    members = s.client.get(prefix + "/members", headers=s.headers)
    assert members.status_code == 200 and len(members.json()) == 2
    added = s.client.post(
        prefix + "/members", headers=s.headers, json={"user_id": s.outsider["id"]}
    )
    assert added.status_code == 200
    assert (
        s.client.delete(prefix + "/members/" + s.outsider["id"], headers=s.headers).status_code
        == 200
    )
    assert (
        s.client.post(
            prefix + "/members",
            headers=s.headers,
            json={"user_id": s.outsider["id"], "role": "owner"},
        ).status_code
        == 422
    )
    response = s.client.get(prefix + "/documents/doc/content", headers=s.headers)
    assert response.content == b"source evidence"
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "attachment" in response.headers["content-disposition"]
    assert (
        s.client.delete(prefix + "/documents/doc", headers=s.headers).json()["status"] == "queued"
    )
    assert s.client.get(prefix + "/jobs", headers=s.headers).json()[0]["id"] == "job"
    assert s.client.post(prefix + "/jobs/1/retry", headers=s.headers).json()["status"] == "queued"
    assert (
        s.client.post(prefix + "/search", headers=s.headers, json={"question": "evidence"}).json()[
            0
        ]["text"]
        == "evidence"
    )
    assert (
        s.client.post(
            prefix + "/search", headers=s.headers, json={"question": "evidence", "k": 101}
        ).status_code
        == 422
    )
    for suffix in ("wiki", "wiki/page", "graph/search?q=entity", "graph/entity/neighbors"):
        assert s.client.get(prefix + "/" + suffix, headers=s.headers).status_code == 200
    assert (
        s.client.get(prefix + "/graph/entity/neighbors?depth=4", headers=s.headers).status_code
        == 422
    )
    conversation = s.client.post(
        prefix + "/conversations", headers=s.headers, json={"title": "Chat"}
    ).json()
    cid = conversation["id"]
    response = s.client.post(
        prefix + f"/conversations/{cid}/messages", headers=s.headers, json={"question": "Question"}
    )
    assert response.status_code == 200
    assert response.json()["sources"][0]["doc_id"] == "doc"
    messages = s.client.get(prefix + f"/conversations/{cid}/messages", headers=s.headers).json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert s.client.get(prefix + "/conversations", headers=s.headers).json()[0]["id"] == cid


def test_membership_revoked_during_await_does_not_disclose_result(setup_api):
    s = setup_api

    async def search(ctx, question, mode="rag", k=8):
        s.store.remove_member(s.team["id"], s.admin["id"])
        return [{"text": "private evidence"}]

    s.service.search = search
    result = s.client.post(
        f"/api/teams/{s.team['id']}/search", headers=s.headers, json={"question": "Question"}
    )
    assert result.status_code == 403
    assert "private evidence" not in result.text


def test_validation_does_not_echo_password(setup_api):
    s = setup_api
    password = "private-secret-" * 100
    response = s.client.post("/api/auth/login", json={"username": "admin", "password": password})
    assert response.status_code == 422
    assert password not in response.text


def test_factory_uses_injected_service_settings(tmp_path, monkeypatch):
    import time

    from fastapi.testclient import TestClient

    from mnogobase.api.app import create_api
    from mnogobase.config import Settings
    from mnogobase.service.runtime import TeamService

    service = TeamService(Settings(service={"root": tmp_path / "custom", "session_hours": 1}))
    service.store.create_user("alice", "long-enough-password")
    api = TestClient(create_api(service=service))
    result = api.post(
        "/api/auth/login", json={"username": "alice", "password": "long-enough-password"}
    )
    token = result.json()["token"]
    future = time.time() + 2 * 3600
    monkeypatch.setattr("mnogobase.service.store.time.time", lambda: future)
    assert api.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


async def test_slow_upload_does_not_block_api_event_loop(setup_api):
    import asyncio
    import threading
    import time

    import httpx

    s = setup_api
    entered = threading.Event()
    release = threading.Event()

    def upload(ctx, filename, content):
        entered.set()
        release.wait(timeout=3)
        return {"id": "job", "status": "pending"}

    s.service.upload = upload
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=s.client.app), base_url="http://testserver"
    ) as client:
        started = time.monotonic()
        task = asyncio.create_task(
            client.post(
                f"/api/teams/{s.team['id']}/documents?filename=notes.md",
                headers=s.headers,
                content=b"notes",
            )
        )
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            assert time.monotonic() - started < 1.5
            health = await asyncio.wait_for(client.get("/health"), timeout=1)
            assert health.status_code == 200
            assert not task.done()
        finally:
            release.set()
            response = await task
        assert response.status_code == 202


def test_source_response_uses_snapshot_when_file_is_deleted(setup_api):
    from urllib.parse import quote

    s = setup_api
    path = s.tmp_path / "source.md"
    path.write_bytes(b"Atomic source evidence")
    filename = 'отчёт "доказательство".md'

    def old_path(ctx, doc_id):
        path.unlink()
        return path

    def snapshot(ctx, doc_id):
        content = path.read_bytes()
        path.unlink()
        return content, filename

    s.service.source = old_path
    s.service.read_source = snapshot
    response = s.client.get(f"/api/teams/{s.team['id']}/documents/doc/content", headers=s.headers)
    assert response.status_code == 200
    assert response.content == b"Atomic source evidence"
    assert not path.exists()
    assert response.headers["content-disposition"] == "attachment; filename*=utf-8''" + quote(
        filename, safe=""
    )
    assert response.headers["content-type"] == "application/octet-stream"


@pytest.mark.parametrize("revoke", ["session", "membership"])
def test_source_snapshot_rechecks_access_before_returning_bytes(setup_api, revoke):
    s = setup_api
    path = s.tmp_path / "source.md"
    path.write_bytes(b"Private source evidence")
    s.service.source = lambda ctx, doc_id: path

    def snapshot(ctx, doc_id):
        if revoke == "session":
            s.store.logout(s.token)
        else:
            s.store.remove_member(s.team["id"], s.admin["id"])
        return b"Private source evidence", "source.md"

    s.service.read_source = snapshot
    response = s.client.get(f"/api/teams/{s.team['id']}/documents/doc/content", headers=s.headers)
    assert response.status_code == (401 if revoke == "session" else 403)
    assert b"Private source evidence" not in response.content


def test_retry_job_id_requires_positive_integer(setup_api):
    s = setup_api
    s.service.retry_job = lambda ctx, job_id: {"id": job_id}
    prefix = f"/api/teams/{s.team['id']}/jobs"
    for job_id in ("0", "-1", "not-an-id"):
        assert s.client.post(prefix + f"/{job_id}/retry", headers=s.headers).status_code == 422
    response = s.client.post(prefix + "/1/retry", headers=s.headers)
    assert response.status_code == 200
    assert response.json()["id"] == 1
