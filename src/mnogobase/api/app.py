from __future__ import annotations

# FastAPI evaluates these dependency declarations while registering routes.
# ruff: noqa: B008
import hashlib
import os
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path as FilePath
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mnogobase.core.config import Settings, load_settings
from mnogobase.service.errors import ServiceError

Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0, le=1_000_000)]
Mode = Literal["rag", "wiki", "graph", "all"]
MAX_JSON_BODY = 64 * 1024


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=16000)
    mode: Mode = "rag"


class Search(Question):
    k: int = Field(default=8, ge=1, le=100)


class Conversation(BaseModel):
    title: str = Field(default="", max_length=200)


class BodyLimit:
    """413 для тела больше 64 KiB до разбора JSON и до проверки сессии.

    Исключение — загрузка документа: её предел (`service.max_upload_mb`) проверяет
    обработчик потоково."""

    def __init__(self, app: ASGIApp, limit: int = MAX_JSON_BODY):
        self.app, self.limit = app, limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or (
            scope["method"] == "POST" and scope["path"] == "/api/documents"
        ):
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers", []):
            if name == b"content-length" and value.isdigit() and int(value) > self.limit:
                await _too_large(send)
                return
        received = 0

        async def limited() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.limit:
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, limited, send)
        except _BodyTooLarge:
            await _too_large(send)


class _BodyTooLarge(Exception):
    pass


async def _too_large(send: Send) -> None:
    response = JSONResponse({"detail": "Request body too large"}, status_code=413)
    await send({"type": "http.response.start", "status": 413, "headers": response.raw_headers})
    await send({"type": "http.response.body", "body": response.body})


def get_kb(request: Request) -> Any:
    """Единственная зависимость, через которую обработчики получают базу знаний."""
    return request.app.state.kb


def bearer(authorization: Annotated[str | None, Header()] = None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise ServiceError(401, "Authentication required")
    token = authorization[7:]
    if not token or any(c.isspace() for c in token):
        raise ServiceError(401, "Authentication required")
    return token


def current_user(request: Request, token: str = Depends(bearer)) -> dict:
    return get_kb(request).store.authenticate(token)


KB = Annotated[Any, Depends(get_kb)]
User = Annotated[dict, Depends(current_user)]


def create_api(settings: Settings | None = None, *, kb=None) -> FastAPI:
    """Создаёт API без изменения cwd, окружения и общих настроек."""
    if settings is None:
        settings = kb.settings if kb is not None else load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = app.state.kb is None
        if owned:
            from mnogobase.service.kb import KnowledgeBase

            app.state.kb = await run_in_threadpool(KnowledgeBase, settings)
        try:
            yield
        finally:
            if owned:
                await app.state.kb.close()

    app = FastAPI(
        title="mnogobase",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.kb = kb
    app.add_middleware(BodyLimit)

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, exc: RequestValidationError):
        # Validation errors must never echo a submitted password/token/body.
        errors = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return JSONResponse({"detail": errors}, status_code=422)

    @app.exception_handler(Exception)
    async def unavailable(request: Request, exc: Exception):
        return JSONResponse({"detail": "Service temporarily unavailable"}, status_code=503)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/auth/login")
    def login(body: Login, kb: KB):
        return {
            "token": kb.store.login(
                body.username, body.password, ttl_hours=settings.service.session_hours
            ),
            "token_type": "bearer",
        }

    @app.post("/api/auth/logout")
    def logout(kb: KB, user: User, token: str = Depends(bearer)):
        kb.store.logout(token)
        return {"status": "ok"}

    @app.get("/api/auth/me")
    def me(user: User):
        return {key: user[key] for key in ("id", "username")}

    @app.get("/api/documents")
    async def documents(kb: KB, user: User, limit: Limit = 50, offset: Offset = 0):
        return await kb.documents(limit=limit, offset=offset)

    @app.post("/api/documents", status_code=202)
    async def upload(
        request: Request,
        kb: KB,
        user: User,
        filename: Annotated[str, Query(min_length=1, max_length=255)],
    ):
        kb.upload_suffix(filename)  # имя и тип — до чтения тела
        maximum = settings.service.max_upload_mb * 1024 * 1024
        length = request.headers.get("content-length")
        if length is not None:
            try:
                length_value = int(length)
            except ValueError as exc:
                raise ServiceError(422, "Invalid content length") from exc
            if length_value < 0:
                raise ServiceError(422, "Invalid content length")
            if length_value > maximum:
                raise ServiceError(413, "Upload too large")
        # поток сразу во временный файл с подсчётом sha256: тело не копится в памяти
        handle, name = tempfile.mkstemp(dir=kb.staging_dir(), prefix=".upload-")
        staged = FilePath(name)
        try:
            digest, size = hashlib.sha256(), 0
            with os.fdopen(handle, "wb") as out:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > maximum:
                        raise ServiceError(413, "Upload too large")
                    digest.update(chunk)
                    await run_in_threadpool(out.write, chunk)
            return await run_in_threadpool(kb.upload_file, filename, staged, digest.hexdigest())
        finally:
            staged.unlink(missing_ok=True)

    @app.get("/api/documents/{doc_id}/content")
    def source(doc_id: str, kb: KB, user: User):
        content, filename = kb.read_source(doc_id)
        return Response(
            content=content,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": "attachment; filename*=utf-8''" + quote(filename, safe=""),
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'",
            },
        )

    @app.delete("/api/documents/{doc_id}")
    async def delete_document(doc_id: str, kb: KB, user: User):
        return await kb.remove_document(doc_id)

    @app.get("/api/jobs")
    def jobs(kb: KB, user: User, limit: Limit = 50, offset: Offset = 0):
        return kb.jobs(limit=limit, offset=offset)

    @app.post("/api/jobs/{job_id}/retry")
    def retry_job(job_id: Annotated[int, Path(ge=1, le=9223372036854775807)], kb: KB, user: User):
        return kb.retry_job(job_id)

    @app.post("/api/search")
    async def search(body: Search, kb: KB, user: User):
        return await kb.search(body.question, mode=body.mode, k=body.k)

    @app.post("/api/ask")
    async def ask(body: Question, kb: KB, user: User):
        return await kb.ask(body.question, mode=body.mode)

    @app.post("/api/conversations", status_code=201)
    def create_conversation(body: Conversation, kb: KB, user: User):
        return kb.store.create_conversation(user["id"], body.title)

    @app.get("/api/conversations")
    def conversations(kb: KB, user: User, limit: Limit = 50, offset: Offset = 0):
        return kb.store.conversations(user["id"], limit=limit, offset=offset)

    @app.get("/api/conversations/{conversation_id}/messages")
    def history(conversation_id: str, kb: KB, user: User, limit: Limit = 100, offset: Offset = 0):
        return kb.history(user["id"], conversation_id, limit=limit, offset=offset)

    @app.post("/api/conversations/{conversation_id}/messages")
    async def send_message(conversation_id: str, body: Question, kb: KB, user: User):
        return await kb.send_message(user["id"], conversation_id, body.question, mode=body.mode)

    @app.get("/api/wiki")
    async def wiki_pages(kb: KB, user: User, limit: Limit = 50, offset: Offset = 0):
        return await kb.wiki_pages(limit=limit, offset=offset)

    @app.get("/api/wiki/{page_id}")
    async def wiki_page(page_id: str, kb: KB, user: User):
        return await kb.wiki_page(page_id)

    @app.get("/api/graph/search")
    async def graph_search(
        kb: KB,
        user: User,
        q: Annotated[str, Query(min_length=1, max_length=1000)],
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ):
        return await kb.graph_search(q, limit=limit)

    @app.get("/api/graph/{entity_id}/neighbors")
    async def graph_neighbors(
        entity_id: str,
        kb: KB,
        user: User,
        depth: Annotated[int, Query(ge=1, le=3)] = 1,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ):
        return await kb.graph_neighbors(entity_id, depth=depth, limit=limit)

    @app.get("/api/status")
    def status(kb: KB, user: User):
        return kb.status()

    return app
