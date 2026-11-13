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

    async def wiki_pages(limit=50, offset=0, q=None):
        return [{"page_id": "p"}]

    async def wiki_page(page_id):
        if page_id != "p":
            raise ServiceError(404, "Страница wiki не найдена")
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

    async def history(user_id, cid, limit=100, offset=0, newest=False):
        return kb.store.messages(user_id, cid)

    kb.history = history
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

    async def history(user_id, conversation_id, limit=100, offset=0, newest=False):
        return api.store.messages(user_id, conversation_id)

    api.kb.history = history
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


ORIGIN = {"Origin": "https://testserver"}


def browser(api, settings=None):
    """Клиент как браузер: https (Secure-cookie уходит только по https) и общий cookie-jar."""
    app = create_api(settings or api.settings, kb=api.kb)
    return TestClient(app, base_url="https://testserver", raise_server_exceptions=False)


def cookie_login(client, password=PASSWORD):
    body = {"username": "owner", "password": password, "cookie": True}
    return client.post("/api/auth/login", json=body, headers=ORIGIN)


def test_cookie_login_sets_httponly_cookie_without_token(api):
    client = browser(api)
    response = cookie_login(client)
    assert response.status_code == 200
    assert response.json() == {"username": "owner"}
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("mb_session=")
    lowered = cookie.lower()
    for part in ("httponly", "samesite=strict", "secure", "path=/", "max-age="):
        assert part in lowered
    assert client.get("/api/auth/me").json()["username"] == "owner"


def test_cookie_secure_can_be_disabled(api):
    settings = api.settings.model_copy(
        update={"service": api.settings.service.model_copy(update={"cookie_secure": False})}
    )
    response = cookie_login(browser(api, settings))
    assert "secure" not in response.headers["set-cookie"].lower()


def test_cookie_login_needs_same_origin(api):
    client = browser(api)
    body = {"username": "owner", "password": PASSWORD, "cookie": True}
    assert client.post("/api/auth/login", json=body).status_code == 403
    evil = {"Origin": "https://evil.example"}
    assert client.post("/api/auth/login", json=body, headers=evil).status_code == 403


def test_cookie_unsafe_methods_need_same_origin(api):
    client = browser(api)
    cookie_login(client)
    assert client.post("/api/conversations", json={}).status_code == 403
    evil = {"Origin": "https://evil.example"}
    assert client.post("/api/conversations", json={}, headers=evil).status_code == 403
    assert client.post("/api/conversations", json={}, headers=ORIGIN).status_code == 201
    # чтение без Origin разрешено
    assert client.get("/api/conversations").status_code == 200


def test_bearer_requests_skip_origin_check(api):
    response = api.client.post("/api/conversations", json={}, headers=api.headers)
    assert response.status_code == 201


def test_logout_clears_cookie(api):
    client = browser(api)
    cookie_login(client)
    response = client.post("/api/auth/logout", headers=ORIGIN)
    assert response.status_code == 200
    assert 'mb_session=""' in response.headers["set-cookie"] or "max-age=0" in (
        response.headers["set-cookie"].lower()
    )
    assert client.get("/api/auth/me").status_code == 401


def test_change_password(api):
    client = browser(api)
    cookie_login(client)
    new = "another-long-password"
    wrong = client.post(
        "/api/auth/password",
        json={"old_password": "wrong-password-x", "new_password": new},
        headers=ORIGIN,
    )
    assert wrong.status_code == 401
    short = client.post(
        "/api/auth/password",
        json={"old_password": PASSWORD, "new_password": "short"},
        headers=ORIGIN,
    )
    assert short.status_code in (400, 422)
    done = client.post(
        "/api/auth/password",
        json={"old_password": PASSWORD, "new_password": new},
        headers=ORIGIN,
    )
    assert done.status_code == 200
    # прочие сессии отозваны, а этот браузер получил новую
    assert api.client.get("/api/auth/me", headers=api.headers).status_code == 401
    assert client.get("/api/auth/me").status_code == 200
    assert cookie_login(browser(api), new).status_code == 200
    assert cookie_login(browser(api)).status_code == 401


def test_change_password_with_bearer(api):
    response = api.client.post(
        "/api/auth/password",
        json={"old_password": PASSWORD, "new_password": "another-long-password"},
        headers=api.headers,
    )
    assert response.status_code == 200 and "set-cookie" not in response.headers
    assert api.client.get("/api/auth/me", headers=api.headers).status_code == 401


PAGE_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
    "form-action 'self'"
)


def test_index_and_static_have_security_headers(api):
    index = api.client.get("/")
    assert index.status_code == 200
    assert index.headers["content-type"].startswith("text/html")
    script = api.client.get("/static/app.js")
    assert script.status_code == 200
    for response in (index, script):
        assert response.headers["content-security-policy"] == PAGE_CSP
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
    for path in ("/static/../pyproject.toml", "/static/%2e%2e/api/app.py", "/static/nope.js"):
        assert api.client.get(path).status_code == 404


def test_api_responses_are_not_cached(api):
    assert api.client.get("/health").headers["cache-control"] == "no-store"
    me = api.client.get("/api/auth/me", headers=api.headers)
    assert me.headers["cache-control"] == "no-store"
    assert "content-security-policy" not in me.headers


def test_index_has_no_inline_script_or_style(api):
    import re

    html = api.client.get("/").text
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)
    assert " style=" not in html and "<style" not in html


def test_rename_and_delete_conversations(api):
    store, owner, other = api.store, api.owner, api.other
    chat = store.create_conversation(owner["id"], "")
    renamed = store.rename_conversation(owner["id"], chat["id"], "  Про внимание  ")
    assert renamed["title"] == "Про внимание" and "seq" not in renamed
    with pytest.raises(ServiceError) as foreign:
        store.rename_conversation(other["id"], chat["id"], "x")
    assert foreign.value.status == 404
    store.append_message(owner["id"], chat["id"], "user", "q")
    with pytest.raises(ServiceError):
        store.delete_conversation(other["id"], chat["id"])
    store.delete_conversation(owner["id"], chat["id"])
    with pytest.raises(ServiceError) as gone:
        store.messages(owner["id"], chat["id"])
    assert gone.value.status == 404


def test_title_is_set_only_when_empty(api):
    chat = api.store.create_conversation(api.owner["id"], "")
    api.store.set_title_if_empty(api.owner["id"], chat["id"], "Первый вопрос")
    api.store.set_title_if_empty(api.owner["id"], chat["id"], "Второй вопрос")
    assert api.store.conversation(api.owner["id"], chat["id"])["title"] == "Первый вопрос"


def test_message_meta_round_trips_and_migration_is_idempotent(api):
    chat = api.store.create_conversation(api.owner["id"])
    saved = api.store.append_message(
        api.owner["id"], chat["id"], "assistant", "a", meta={"latency_ms": 5}
    )
    assert saved["meta"] == {"latency_ms": 5}
    api.store.append_message(api.owner["id"], chat["id"], "user", "q")
    again = ServiceStore(database_target(api.settings))  # миграции второй раз — без ошибок
    metas = [m["meta"] for m in again.messages(api.owner["id"], chat["id"])]
    assert metas == [{"latency_ms": 5}, None]


def test_conversation_routes(api):
    h = api.headers
    cid = api.client.post("/api/conversations", headers=h, json={}).json()["id"]
    patched = api.client.patch(f"/api/conversations/{cid}", headers=h, json={"title": "Новое"})
    assert patched.status_code == 200 and patched.json()["title"] == "Новое"
    assert api.client.delete(f"/api/conversations/{cid}", headers=h).json() == {"status": "ok"}
    assert api.client.delete(f"/api/conversations/{cid}", headers=h).status_code == 404
    assert api.client.patch(f"/api/conversations/{cid}", json={"title": "x"}).status_code == 401
    assert api.client.delete(f"/api/conversations/{cid}").status_code == 401


def test_document_and_chunk_routes(api):
    h, doc = api.headers, "d" * 16

    async def document(doc_id):
        return {"id": doc_id, "stages": []}

    async def document_chunks(doc_id, limit=50, offset=0):
        return {"chunks": [], "total": 0, "limit": limit, "offset": offset}

    async def chunk(chunk_id):
        return {"chunk_id": chunk_id}

    api.kb.document, api.kb.document_chunks, api.kb.chunk = document, document_chunks, chunk
    assert api.client.get(f"/api/documents/{doc}", headers=h).json()["id"] == doc
    page = api.client.get(f"/api/documents/{doc}/chunks?limit=5&offset=10", headers=h).json()
    assert (page["limit"], page["offset"]) == (5, 10)
    assert api.client.get(f"/api/documents/{doc}/chunks?limit=500", headers=h).status_code == 422
    assert api.client.get(f"/api/chunks/{doc}:00001", headers=h).json()["chunk_id"].endswith("1")
    for path in (f"/api/documents/{doc}", f"/api/documents/{doc}/chunks", f"/api/chunks/{doc}:1"):
        assert api.client.get(path).status_code == 401


def test_service_checks_run_one_at_a_time_and_hide_failure_details(api, monkeypatch):
    import threading

    from mnogobase.ops.doctor import Check
    from mnogobase.service import kb as kb_module

    started, release = threading.Event(), threading.Event()

    def checks(settings, timeout=5.0, only=None, **kwargs):
        assert timeout == 5 and "llm" in only and "types" not in only
        started.set()
        release.wait(5)
        return [Check("database", True, "ok"), Check("llm", False, "http://secret:8000 down")]

    monkeypatch.setattr(kb_module, "run_checks", checks)
    api.kb.service_checks = partial(KnowledgeBase.service_checks, api.kb)
    result = {}
    worker = threading.Thread(target=lambda: result.update(r=api.kb.service_checks()))
    worker.start()
    assert started.wait(5)
    busy = api.client.get("/api/status/checks", headers=api.headers)
    assert busy.status_code == 429
    release.set()
    worker.join(5)
    by_name = {c["name"]: c for c in result["r"]}
    assert by_name["database"] == {"name": "database", "ok": True, "detail": "ok", "warn": False}
    assert by_name["llm"]["ok"] is False and "secret" not in by_name["llm"]["detail"]
    assert api.client.get("/api/status/checks").status_code == 401


def test_wiki_and_graph_card_routes(api):
    h = api.headers

    async def wiki_pages(limit=50, offset=0, q=None):
        return [{"q": q}]

    async def by_slug(slug):
        return {"slug": slug}

    async def entity(entity_id):
        return {"entity_id": entity_id}

    async def graph_analysis():
        return {"ready": False}

    api.kb.wiki_pages, api.kb.wiki_page_by_slug = wiki_pages, by_slug
    api.kb.entity, api.kb.graph_analysis = entity, graph_analysis
    assert api.client.get("/api/wiki?q=adam", headers=h).json() == [{"q": "adam"}]
    assert api.client.get("/api/wiki/by-slug/adam", headers=h).json() == {"slug": "adam"}
    assert api.client.get("/api/graph/analysis", headers=h).json() == {"ready": False}
    assert api.client.get("/api/graph/e1", headers=h).json() == {"entity_id": "e1"}
    for path in ("/api/wiki/by-slug/adam", "/api/graph/analysis", "/api/graph/e1"):
        assert api.client.get(path).status_code == 401


def test_stream_route_emits_server_sent_events(api):
    import json

    async def send_message_stream(user_id, conversation_id, question, mode="rag"):
        if conversation_id == "missing":
            raise ServiceError(404, "Диалог не найден")

        async def events():
            yield {"event": "sources", "data": [{"n": 1}]}
            yield {"event": "delta", "data": {"text": "При"}}
            yield {"event": "done", "data": {"content": "Привет [1]"}}

        return events()

    api.kb.send_message_stream = send_message_stream
    response = api.client.post(
        "/api/conversations/c1/messages/stream", json={"question": "q"}, headers=api.headers
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    blocks = [b for b in response.text.split("\n\n") if b]
    parsed = [
        (b.split("\n")[0].removeprefix("event: "), json.loads(b.split("\n")[1][6:])) for b in blocks
    ]
    assert parsed == [
        ("sources", [{"n": 1}]),
        ("delta", {"text": "При"}),
        ("done", {"content": "Привет [1]"}),
    ]
    missing = api.client.post(
        "/api/conversations/missing/messages/stream", json={"question": "q"}, headers=api.headers
    )
    assert missing.status_code == 404 and missing.json()["detail"] == "Диалог не найден"
    client = browser(api)
    cookie_login(client)
    no_origin = client.post("/api/conversations/c1/messages/stream", json={"question": "q"})
    assert no_origin.status_code == 403


def test_analyze_route(api):
    api.kb.enqueue_analyze = lambda: {"id": 9, "kind": "analyze"}
    response = api.client.post("/api/graph/analyze", headers=api.headers)
    assert response.status_code == 202 and response.json()["id"] == 9
    assert api.client.post("/api/graph/analyze").status_code == 401
