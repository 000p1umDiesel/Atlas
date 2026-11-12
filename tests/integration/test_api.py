"""HTTP-слой: авторизация, лимиты, безопасные ответы и все маршруты (ядро заменено заглушкой)."""

from __future__ import annotations

import hashlib
from functools import partial
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mnogobase.api.app import create_api
from mnogobase.core.config import Settings
from mnogobase.core.database import database_target
from mnogobase.service.errors import ServiceError
from mnogobase.service.kb import KnowledgeBase
from mnogobase.service.store import ServiceStore

pytestmark = pytest.mark.integration
PASSWORD = "long-enough-password"


@pytest.fixture
def api(tmp_path):
    settings = Settings(service={"max_upload_mb": 1, "uploads_dir": tmp_path / "uploads"})
    store = ServiceStore(database_target(settings))
    owner = store.create_user("owner", PASSWORD)
    other = store.create_user("other", PASSWORD)
    kb = SimpleNamespace(
        settings=settings, store=store, uploads=SimpleNamespace(root=tmp_path / "uploads")
    )
    kb.upload_suffix = partial(KnowledgeBase.upload_suffix, kb)
    kb.staging_dir = partial(KnowledgeBase.staging_dir, kb)
    client = TestClient(create_api(settings, kb=kb), raise_server_exceptions=False)
    headers = {"Authorization": "Bearer " + store.login("owner", PASSWORD)}
    return SimpleNamespace(**locals())


def test_login_me_logout(api):
    assert api.client.get("/api/auth/me").status_code == 401
    assert api.client.get("/api/auth/me", headers={"Authorization": "Bearer"}).status_code == 401
    bad = api.client.post("/api/auth/login", json={"username": "owner", "password": "wrong"})
    assert bad.status_code == 401
    token = api.client.post(
        "/api/auth/login", json={"username": "owner", "password": PASSWORD}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    me = api.client.get("/api/auth/me", headers=headers).json()
    assert me == {"id": api.owner["id"], "username": "owner"}
    assert api.client.post("/api/auth/logout", headers=headers).status_code == 200
    assert api.client.get("/api/auth/me", headers=headers).status_code == 401


def test_validation_errors_do_not_echo_secrets(api):
    response = api.client.post("/api/auth/login", json={"username": "", "password": "secret-pw"})
    assert response.status_code == 422 and "secret-pw" not in response.text


def test_large_bodies_are_rejected_before_parsing_and_auth(api):
    big = {"question": "x" * 70_000}
    assert api.client.post("/api/ask", json=big).status_code == 413
    assert api.client.post("/api/auth/login", content=b"{" + b" " * 70_000).status_code == 413
    # без Content-Length (chunked) предел срабатывает при чтении тела, код тот же
    chunked = (part for part in [b"{", b" " * 70_000, b"}"])
    headers = {"content-type": "application/json"}
    response = api.client.post("/api/auth/login", content=chunked, headers=headers)
    assert response.status_code == 413
    # загрузка документа — исключение: её предел проверяет сам обработчик
    assert api.client.post("/api/documents?filename=a.md", content=b"x" * 70_000).status_code == 401


def test_docs_and_server_details_are_hidden(api):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert api.client.get(path).status_code == 404
    api.kb.jobs = lambda limit=50, offset=0: (_ for _ in ()).throw(RuntimeError("dsn secret"))
    response = api.client.get("/api/jobs", headers=api.headers)
    assert response.status_code == 503 and "secret" not in response.text


def test_upload_limits_and_forwarding(api):
    seen = []

    def upload_file(name, staged, sha256):
        seen.append((name, staged.read_bytes(), sha256))
        return {"id": 1, "status": "pending"}

    api.kb.upload_file = upload_file
    response = api.client.post(
        "/api/documents?filename=a.md", content=b"hello", headers=api.headers
    )
    assert response.status_code == 202
    assert seen == [("a.md", b"hello", hashlib.sha256(b"hello").hexdigest())]
    too_big = api.client.post(
        "/api/documents?filename=a.md", content=b"x" * (1024 * 1024 + 1), headers=api.headers
    )
    assert too_big.status_code == 413
    wrong_type = api.client.post(
        "/api/documents?filename=a.exe", content=b"MZ", headers=api.headers
    )
    assert wrong_type.status_code == 422 and len(seen) == 1
    # временные файлы загрузки не остаются ни после успеха, ни после отказа
    assert not list(api.kb.staging_dir().glob(".upload-*"))


def test_all_routes(api, tmp_path):
    kb, h = api.kb, api.headers

    async def documents(limit=50, offset=0):
        return [{"id": "d" * 16, "name": "a.md"}]

    async def remove(doc_id):
        return {"id": 2, "kind": "remove"}

    async def search(question, mode="rag", k=8):
        return [{"text": "evidence", "mode": mode, "k": k}]

    async def ask(question, mode="rag"):
        return {"text": "answer [1]", "sources": [{"url": "/api/documents/x/content"}]}

    async def send(user_id, conversation_id, question, mode="rag"):
        kb.store.append_message(user_id, conversation_id, "user", question)
        return kb.store.append_message(user_id, conversation_id, "assistant", "reply")

    async def wiki_pages(limit=50, offset=0):
        return [{"page_id": "p"}]

    async def wiki_page(page_id):
        if page_id != "p":
            raise ServiceError(404, "Wiki page not found")
        return {"id": "p", "content": "text"}

    async def graph_search(query, limit=20):
        return [{"entity_id": "e"}]

    async def graph_neighbors(entity_id, depth=1, limit=50):
        return {"nodes": [], "relations": [], "depth": depth}

    kb.documents, kb.remove_document, kb.search, kb.ask = documents, remove, search, ask
    kb.send_message, kb.wiki_pages, kb.wiki_page = send, wiki_pages, wiki_page
    kb.graph_search, kb.graph_neighbors = graph_search, graph_neighbors
    kb.read_source = lambda doc_id: (b"<script>", "отчёт.html")
    kb.jobs = lambda limit=50, offset=0: [{"id": 1}]
    kb.retry_job = lambda job_id: {"id": job_id, "status": "pending"}
    kb.history = lambda user_id, cid, limit=100, offset=0: kb.store.messages(user_id, cid)
    kb.status = lambda: {"queue": {}}

    assert api.client.get("/health").json() == {"status": "ok"}
    assert api.client.get("/api/documents", headers=h).json()[0]["name"] == "a.md"
    assert api.client.get("/api/documents?limit=0", headers=h).status_code == 422
    source = api.client.get(f"/api/documents/{'d' * 16}/content", headers=h)
    assert source.content == b"<script>"
    assert source.headers["content-type"] == "application/octet-stream"
    assert source.headers["x-content-type-options"] == "nosniff"
    assert source.headers["content-security-policy"] == "default-src 'none'"
    assert "filename*=utf-8''%D0%BE%D1%82" in source.headers["content-disposition"]
    assert api.client.delete(f"/api/documents/{'d' * 16}", headers=h).json()["kind"] == "remove"
    assert api.client.get("/api/jobs", headers=h).json() == [{"id": 1}]
    assert api.client.post("/api/jobs/5/retry", headers=h).json()["id"] == 5
    assert api.client.post("/api/jobs/0/retry", headers=h).status_code == 422
    found = api.client.post("/api/search", headers=h, json={"question": "q", "mode": "wiki"})
    assert found.json()[0] == {"text": "evidence", "mode": "wiki", "k": 8}
    assert (
        api.client.post("/api/search", headers=h, json={"question": "q", "k": 101}).status_code
        == 422
    )
    assert (
        api.client.post("/api/ask", headers=h, json={"question": "q", "mode": "x"}).status_code
        == 422
    )
    assert api.client.post("/api/ask", headers=h, json={"question": "q"}).json()["text"]
    chat = api.client.post("/api/conversations", headers=h, json={"title": "Chat"})
    assert chat.status_code == 201
    cid = chat.json()["id"]
    reply = api.client.post(f"/api/conversations/{cid}/messages", headers=h, json={"question": "q"})
    assert reply.json()["content"] == "reply"
    messages = api.client.get(f"/api/conversations/{cid}/messages", headers=h).json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert api.client.get("/api/conversations", headers=h).json()[0]["id"] == cid
    assert api.client.get("/api/wiki", headers=h).json() == [{"page_id": "p"}]
    assert api.client.get("/api/wiki/p", headers=h).json()["content"] == "text"
    assert api.client.get("/api/wiki/missing", headers=h).status_code == 404
    assert api.client.get("/api/graph/search?q=x", headers=h).json() == [{"entity_id": "e"}]
    assert api.client.get("/api/graph/e/neighbors?depth=2", headers=h).json()["depth"] == 2
    assert api.client.get("/api/graph/e/neighbors?depth=4", headers=h).status_code == 422
    assert api.client.get("/api/status", headers=h).json() == {"queue": {}}
    # без сессии ни один маршрут не отвечает данными
    for method, path in [
        ("get", "/api/documents"),
        ("get", "/api/jobs"),
        ("post", "/api/ask"),
        ("get", "/api/wiki"),
        ("get", "/api/status"),
    ]:
        assert getattr(api.client, method)(path).status_code == 401


def test_conversations_are_private(api):
    other = {"Authorization": "Bearer " + api.store.login("other", PASSWORD)}
    cid = api.client.post("/api/conversations", headers=api.headers, json={}).json()["id"]
    api.kb.history = lambda user_id, conversation_id, limit=100, offset=0: api.store.messages(
        user_id, conversation_id
    )
    assert api.client.get(f"/api/conversations/{cid}/messages", headers=other).status_code == 404
    assert api.client.get("/api/conversations", headers=other).json() == []


def test_password_hashing_is_bounded_and_failures_are_logged(api, monkeypatch):
    import threading

    from structlog.testing import capture_logs

    from mnogobase.service import store as store_module

    busy = threading.BoundedSemaphore(1)
    busy.acquire()
    monkeypatch.setattr(store_module, "_ARGON2_SLOTS", busy)
    monkeypatch.setattr(store_module, "_ARGON2_WAIT_S", 0.01)
    body = {"username": "owner", "password": PASSWORD}
    assert api.client.post("/api/auth/login", json=body).status_code == 429
    busy.release()
    with capture_logs() as logs:
        body["password"] = "wrong-password-x"
        assert api.client.post("/api/auth/login", json=body).status_code == 401
    [failed] = [e for e in logs if e["event"] == "login_failed"]
    assert "owner" not in str(failed)
