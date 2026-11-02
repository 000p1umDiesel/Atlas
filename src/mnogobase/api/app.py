from __future__ import annotations

# FastAPI evaluates these dependency declarations while registering routes.
# ruff: noqa: B008
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from mnogobase.config import Settings, load_settings
from mnogobase.service.context import TeamContext, validate_team_id
from mnogobase.service.errors import ServiceError

Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0, le=1_000_000)]
Mode = Literal["rag", "wiki", "graph", "all"]


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class Member(BaseModel):
    user_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    role: Literal["admin", "member"] = "member"


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=16000)
    mode: Mode = "rag"


class Search(Question):
    k: int = Field(default=8, ge=1, le=100)


class Conversation(BaseModel):
    title: str = Field(default="", max_length=200)


def create_api(settings: Settings | None = None, *, service=None) -> FastAPI:
    """Создаёт API без изменения cwd, окружения и общих настроек."""
    if settings is None:
        settings = service.settings if service is not None else load_settings()
    if service is None:
        from mnogobase.service.runtime import TeamService

        service = TeamService(settings)
    app = FastAPI(title="mnogobase team service")
    app.state.service = service
    store = service.store

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

    def bearer(authorization: Annotated[str | None, Header()] = None) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise ServiceError(401, "Authentication required")
        token = authorization[7:]
        if not token or any(c.isspace() for c in token):
            raise ServiceError(401, "Authentication required")
        store.authenticate(token)
        return token

    def context(team_id: str, token: str, *, admin: bool = False) -> TeamContext:
        try:
            validate_team_id(team_id)
        except ServiceError as exc:
            raise ServiceError(422, "Invalid team ID") from exc
        user = store.authenticate(token)
        return store.context(user["id"], team_id, admin=admin)

    async def revalidate(team_id: str, token: str, *, admin: bool = False) -> TeamContext:
        return await run_in_threadpool(context, team_id, token, admin=admin)

    # Dependencies carry current identity only; all long calls revalidate before returning.
    def member_context(team_id: str, token: str = Depends(bearer)) -> TeamContext:
        return context(team_id, token)

    def admin_context(team_id: str, token: str = Depends(bearer)) -> TeamContext:
        return context(team_id, token, admin=True)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/auth/login")
    def login(body: Login):
        return {
            "token": store.login(
                body.username, body.password, ttl_hours=settings.service.session_hours
            ),
            "token_type": "bearer",
        }

    @app.post("/api/auth/logout")
    def logout(token: str = Depends(bearer)):
        store.logout(token)
        return {"status": "ok"}

    @app.get("/api/auth/me")
    def me(token: str = Depends(bearer)):
        user = store.authenticate(token)
        return {key: user[key] for key in ("id", "username")}

    @app.get("/api/teams")
    def teams(token: str = Depends(bearer)):
        return store.teams(store.authenticate(token)["id"])

    @app.get("/api/teams/{team_id}/members")
    def members(team_id: str, ctx: TeamContext = Depends(admin_context)):
        return store.members(team_id)

    @app.post("/api/teams/{team_id}/members")
    def add_member(team_id: str, body: Member, ctx: TeamContext = Depends(admin_context)):
        store.add_member(team_id, body.user_id, body.role)
        return {"status": "ok"}

    @app.delete("/api/teams/{team_id}/members/{user_id}")
    def remove_member(team_id: str, user_id: str, ctx: TeamContext = Depends(admin_context)):
        store.remove_member(team_id, user_id)
        return {"status": "ok"}

    @app.get("/api/teams/{team_id}/documents")
    def documents(
        limit: Limit = 50, offset: Offset = 0, ctx: TeamContext = Depends(member_context)
    ):
        return service.documents(ctx, limit=limit, offset=offset)

    @app.post("/api/teams/{team_id}/documents", status_code=202)
    async def upload(
        request: Request,
        team_id: str,
        filename: Annotated[str, Query(min_length=1, max_length=255)],
        token: str = Depends(bearer),
    ):
        ctx = await revalidate(team_id, token)
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
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > maximum:
                raise ServiceError(413, "Upload too large")
            data.extend(chunk)
        # Uploading the body may itself take long enough for the session to be revoked.
        ctx = await revalidate(team_id, token)
        result = await run_in_threadpool(service.upload, ctx, filename, bytes(data))
        await revalidate(team_id, token)
        return result

    @app.get("/api/teams/{team_id}/documents/{doc_id}/content")
    def source(
        team_id: str,
        doc_id: str,
        token: str = Depends(bearer),
        ctx: TeamContext = Depends(member_context),
    ):
        content, filename = service.read_source(ctx, doc_id)
        context(team_id, token)
        return Response(
            content=content,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": "attachment; filename*=utf-8''" + quote(filename, safe=""),
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'",
            },
        )

    @app.delete("/api/teams/{team_id}/documents/{doc_id}")
    async def delete_document(
        team_id: str,
        doc_id: str,
        token: str = Depends(bearer),
        ctx: TeamContext = Depends(admin_context),
    ):
        result = await service.remove_document(ctx, doc_id)
        await revalidate(team_id, token, admin=True)
        return result

    @app.get("/api/teams/{team_id}/jobs")
    def jobs(limit: Limit = 50, offset: Offset = 0, ctx: TeamContext = Depends(member_context)):
        return service.jobs(ctx, limit=limit, offset=offset)

    @app.post("/api/teams/{team_id}/jobs/{job_id}/retry")
    def retry_job(
        job_id: Annotated[int, Path(ge=1, le=9223372036854775807)],
        ctx: TeamContext = Depends(member_context),
    ):
        return service.retry_job(ctx, job_id)

    @app.post("/api/teams/{team_id}/search")
    async def search(
        team_id: str,
        body: Search,
        token: str = Depends(bearer),
        ctx: TeamContext = Depends(member_context),
    ):
        result = await service.search(ctx, body.question, mode=body.mode, k=body.k)
        await revalidate(team_id, token)
        return result

    @app.post("/api/teams/{team_id}/ask")
    async def ask(
        team_id: str,
        body: Question,
        token: str = Depends(bearer),
        ctx: TeamContext = Depends(member_context),
    ):
        result = await service.ask(ctx, body.question, mode=body.mode)
        await revalidate(team_id, token)
        return result

    @app.post("/api/teams/{team_id}/conversations", status_code=201)
    def create_conversation(body: Conversation, ctx: TeamContext = Depends(member_context)):
        return store.create_conversation(ctx, body.title)

    @app.get("/api/teams/{team_id}/conversations")
    def conversations(
        limit: Limit = 50, offset: Offset = 0, ctx: TeamContext = Depends(member_context)
    ):
        return store.conversations(ctx, limit=limit, offset=offset)

    @app.get("/api/teams/{team_id}/conversations/{conversation_id}/messages")
    def history(
        conversation_id: str,
        limit: Limit = 100,
        offset: Offset = 0,
        ctx: TeamContext = Depends(member_context),
    ):
        # Check ownership even when an alternate runtime implementation is injected.
        store.conversation(ctx, conversation_id)
        return service.history(ctx, conversation_id, limit=limit, offset=offset)

    @app.post("/api/teams/{team_id}/conversations/{conversation_id}/messages")
    async def send_message(
        team_id: str,
        conversation_id: str,
        body: Question,
        token: str = Depends(bearer),
        ctx: TeamContext = Depends(member_context),
    ):
        await run_in_threadpool(store.conversation, ctx, conversation_id)
        result = await service.send_message(ctx, conversation_id, body.question, mode=body.mode)
        current = await revalidate(team_id, token)
        await run_in_threadpool(store.conversation, current, conversation_id)
        return result

    @app.get("/api/teams/{team_id}/wiki")
    def wiki_pages(
        limit: Limit = 50, offset: Offset = 0, ctx: TeamContext = Depends(member_context)
    ):
        return service.wiki_pages(ctx, limit=limit, offset=offset)

    @app.get("/api/teams/{team_id}/wiki/{page_id}")
    def wiki_page(page_id: str, ctx: TeamContext = Depends(member_context)):
        return service.wiki_page(ctx, page_id)

    @app.get("/api/teams/{team_id}/graph/search")
    def graph_search(
        q: Annotated[str, Query(min_length=1, max_length=1000)],
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        ctx: TeamContext = Depends(member_context),
    ):
        return service.graph_search(ctx, q, limit=limit)

    @app.get("/api/teams/{team_id}/graph/{entity_id}/neighbors")
    def graph_neighbors(
        entity_id: str,
        depth: Annotated[int, Query(ge=1, le=3)] = 1,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        ctx: TeamContext = Depends(member_context),
    ):
        return service.graph_neighbors(ctx, entity_id, depth=depth, limit=limit)

    return app
