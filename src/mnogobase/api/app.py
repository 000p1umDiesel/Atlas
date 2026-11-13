"""HTTP API базы знаний для владельца: тонкий слой FastAPI над `KnowledgeBase`.

Обработчики только проверяют вход, достают пользователя и вызывают методы `KnowledgeBase`
(`service/kb.py`); вся логика и доступ к данным — там. Синхронные обработчики FastAPI
выполняет в пуле потоков, асинхронные работают с ядром через поток `CoreLoop`.

Группы маршрутов:
- `/` и `/static/*` — веб-интерфейс (`mnogobase/web/`, CSP `PAGE_CSP`, без inline-кода);
- `/health` — проверка живости без авторизации и без обращения к базам;
- `/api/auth/login|logout|me|password` — вход, отзыв сессии, текущий пользователь, смена
  своего пароля;
- `/api/documents` — список, загрузка (202, задача `ingest`), скачивание исходника,
  удаление (задача `remove`);
- `/api/jobs` — задачи фоновой очереди и их повтор;
- `/api/search`, `/api/ask` — поиск фрагментов и ответ на вопрос;
- `/api/conversations` — личные диалоги (создание, переименование, удаление) и сообщения в
  них (вопрос с учётом истории, в том числе потоком SSE);
- `/api/wiki`, `/api/graph` — страницы wiki, поиск сущностей и их окрестность в графе;
- `/api/status` — очередь, жив ли worker, расход LLM.

Модель безопасности:
- Сессии: `Authorization: Bearer <токен>` (CLI-клиент) или cookie `mb_session` (браузер:
  `HttpOnly; SameSite=Strict; Secure` при `service.cookie_secure`; токен в JS не попадает).
  В базе хранится только sha256 токена, сессию можно отозвать (`logout`, смена пароля из
  веба или CLI, отключение пользователя). Учётные записи создаются только из CLI. Все
  маршруты, кроме `/health` и `/api/auth/login`, требуют сессию.
- CSRF: для cookie-сессии небезопасные методы и вход с `cookie=true` требуют `Origin`,
  совпадающий с `Host` (иначе 403); Bearer-запросы не проверяются.
- `BodyLimit` отвергает тела больше 64 KiB до разбора JSON и до проверки сессии, в том
  числе chunked-тела без `Content-Length`.
- Загрузка документа — исключение из `BodyLimit`: имя и тип проверяются до чтения тела,
  затем тело потоково пишется во временный файл с подсчётом sha256 и проверкой
  `service.max_upload_mb`, так что весь файл никогда не держится в памяти.
- Ошибки: `ServiceError` → JSON `{"detail": ...}` с его кодом; ошибки валидации — 422 без
  присланных значений (там могут быть пароль или токен); всё прочее — 503 без подробностей.
- `/docs`, `/redoc` и `openapi.json` отключены, чтобы не публиковать схему API.
- Исходники отдаются как вложение `application/octet-stream` с `nosniff` и
  `Content-Security-Policy: default-src 'none'`: загруженный HTML или SVG не исполнится в
  браузере в контексте API.
"""

from __future__ import annotations

# FastAPI вычисляет эти объявления зависимостей при регистрации маршрутов (поэтому B008).
# ruff: noqa: B008
import hashlib
import os
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path as FilePath
from typing import Annotated, Any, Literal
from urllib.parse import quote, urlsplit

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mnogobase.core.config import Settings, load_settings
from mnogobase.service.errors import MAX_QUESTION_CHARS, ServiceError

# Пагинация списков: страница до 200 элементов.
Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0, le=1_000_000)]
Mode = Literal["rag", "wiki", "graph", "all"]
MAX_JSON_BODY = 64 * 1024
# cookie веб-сессии (HttpOnly: JS страницы токена не видит)
SESSION_COOKIE = "mb_session"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# статика веб-интерфейса внутри пакета (index.html, app.js, vendor/, fonts/)
WEB_DIR = FilePath(str(files("mnogobase") / "web"))
# CSP страниц: только свои скрипты, стили и шрифты, без inline-кода и CDN (§6.4 аудита)
PAGE_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
    "form-action 'self'"
)
_PAGE_HEADERS = [
    (b"content-security-policy", PAGE_CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
]


class Login(BaseModel):
    """Тело `/api/auth/login`."""

    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)
    # true — сессия в cookie `mb_session` (браузер), токен в ответе не возвращается
    cookie: bool = False


class PasswordChange(BaseModel):
    """Тело `/api/auth/password`: текущий и новый пароль."""

    old_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)


class Question(BaseModel):
    """Тело `/api/ask` и сообщения диалога: вопрос и режим поиска."""

    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    mode: Mode = "rag"


class Search(Question):
    """Тело `/api/search`: вопрос, режим и число фрагментов `k`."""

    k: int = Field(default=8, ge=1, le=100)


class Conversation(BaseModel):
    """Тело создания диалога: необязательный заголовок."""

    title: str = Field(default="", max_length=200)


class BodyLimit:
    """ASGI-middleware: 413 для тела больше 64 KiB до разбора JSON и до проверки сессии.

    Объявленный `Content-Length` больше предела отклоняется сразу, тело не читается. Тело
    без него (chunked) считается по мере чтения: как только сумма превысит предел, чтение
    прерывается `_BodyTooLarge`, и клиент получает 413. Исключение — `POST /api/documents`:
    предел загрузки (`service.max_upload_mb`) проверяет обработчик потоково."""

    def __init__(self, app: ASGIApp, limit: int = MAX_JSON_BODY):
        """`app` — оборачиваемое приложение, `limit` — предел тела в байтах."""
        self.app, self.limit = app, limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Пропускает запрос дальше, подменяя `receive` на считающий байты тела."""
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


class _BodyTooLarge(HTTPException):
    """Тело без Content-Length (chunked) оказалось больше предела уже при чтении.

    Это HTTPException: FastAPI пропускает его из разбора тела как есть, а любое другое
    исключение превратил бы в 400 «error parsing the body»."""

    def __init__(self):
        super().__init__(413, "Тело запроса слишком большое")


async def _too_large(send: Send) -> None:
    """Отправляет ответ 413 напрямую через ASGI `send`, минуя приложение."""
    response = JSONResponse({"detail": "Тело запроса слишком большое"}, status_code=413)
    await send({"type": "http.response.start", "status": 413, "headers": response.raw_headers})
    await send({"type": "http.response.body", "body": response.body})


class SecurityHeaders:
    """ASGI-middleware: заголовки безопасности без буферизации ответа (SSE идёт как есть).

    Страницы и статика (всё, кроме `/api/*` и `/health`) получают CSP `PAGE_CSP`, `nosniff` и
    `Referrer-Policy: no-referrer`; ответы API — `Cache-Control: no-store`. Заголовки, уже
    выставленные обработчиком (например, строгий CSP скачиваемого исходника), не
    перезаписываются."""

    def __init__(self, app: ASGIApp):
        """`app` — оборачиваемое приложение."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Добавляет заголовки в `http.response.start`."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        api = path == "/health" or path == "/api" or path.startswith("/api/")
        extra = [(b"cache-control", b"no-store")] if api else _PAGE_HEADERS

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                headers += [(name, value) for name, value in extra if name not in present]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


def get_kb(request: Request) -> Any:
    """Единственная зависимость, через которую обработчики получают базу знаний."""
    return request.app.state.kb


def _valid_token(token: str | None) -> str:
    """Токен без пробельных символов; 401, если он пустой или содержит пробелы."""
    if not token or any(c.isspace() for c in token):
        raise ServiceError(401, "Требуется вход")
    return token


def session_token(request: Request) -> str:
    """Токен сессии запроса: из `Authorization: Bearer <токен>` (CLI-клиент), а без этого
    заголовка — из cookie `mb_session` (браузер). Источник запоминается в
    `request.state.auth` (`bearer` / `cookie`). 401 — токена нет, схема не Bearer или токен
    пустой либо с пробелами. Сессию не проверяет."""
    authorization = request.headers.get("authorization")
    if authorization is not None:
        if not authorization.startswith("Bearer "):
            raise ServiceError(401, "Требуется вход")
        request.state.auth = "bearer"
        return _valid_token(authorization[7:])
    request.state.auth = "cookie"
    return _valid_token(request.cookies.get(SESSION_COOKIE))


def check_same_origin(request: Request) -> None:
    """403, если `Origin` запроса не совпадает с `Host` (host и порт).

    Защита cookie-сессии от CSRF вместе с `SameSite=Strict`: браузер всегда шлёт `Origin` в
    POST/PATCH/DELETE, а чужой сайт не может подставить наш. Нет `Origin` (или `null`) —
    тоже отказ."""
    origin = request.headers.get("origin") or ""
    host = (request.headers.get("host") or "").lower()
    if not host or urlsplit(origin).netloc.lower() != host:
        raise ServiceError(403, "Запрос с чужого сайта отклонён")


def current_user(request: Request) -> dict:
    """Пользователь текущей сессии (`id`, `username`); 401, если сессия недействительна.

    Для cookie-сессии небезопасные методы (всё, кроме GET/HEAD/OPTIONS) дополнительно
    проверяются `check_same_origin`; запросы с Bearer-токеном — нет (у чужого сайта его
    нет). Синхронная зависимость: FastAPI выполняет её в пуле потоков (запрос к PostgreSQL)."""
    token = session_token(request)
    if request.state.auth == "cookie" and request.method not in _SAFE_METHODS:
        check_same_origin(request)
    return get_kb(request).store.authenticate(token)


KB = Annotated[Any, Depends(get_kb)]
User = Annotated[dict, Depends(current_user)]


def create_api(settings: Settings | None = None, *, kb=None) -> FastAPI:
    """Создаёт приложение FastAPI без изменения cwd, окружения и общих настроек.

    `kb` — готовая `KnowledgeBase` (тесты подставляют свою); тогда приложение её не
    закрывает. Без `kb` база создаётся при старте (`lifespan`, в пуле потоков: создание
    таблиц блокирует) и закрывается при остановке. `settings` по умолчанию — от `kb` или из
    `config.yaml` текущего каталога."""
    if settings is None:
        settings = kb.settings if kb is not None else load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Создаёт `KnowledgeBase` при старте, если её не передали, и закрывает свою при
        остановке."""
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
    app.add_middleware(SecurityHeaders)

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError):
        """`ServiceError` → JSON `{"detail": ...}` с кодом ошибки."""
        return JSONResponse({"detail": exc.detail}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, exc: RequestValidationError):
        """422 со списком ошибок валидации (`loc`, `msg`, `type`)."""
        # Не повторяем присланные значения: среди них могут быть пароль, токен или тело.
        errors = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return JSONResponse({"detail": errors}, status_code=422)

    @app.exception_handler(Exception)
    async def unavailable(request: Request, exc: Exception):
        """Любое непойманное исключение → 503 без подробностей (текст может содержать
        секреты); Starlette при этом пишет трассировку в лог сервера."""
        return JSONResponse({"detail": "Сервис временно недоступен"}, status_code=503)

    @app.get("/")
    def index():
        """Страница веб-интерфейса; маршруты внутри неё — в `#/...`, сервер их не видит."""
        return FileResponse(WEB_DIR / "index.html", media_type="text/html")

    # Starlette сама отвергает выход за каталог (`..`, ссылки наружу) ответом 404.
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/health")
    def health():
        """Проверка живости процесса без авторизации; базы и модели не проверяет."""
        return {"status": "ok"}

    def set_session_cookie(response: Response, token: str) -> None:
        """Ставит cookie веб-сессии на срок `service.session_hours`."""
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=settings.service.session_hours * 3600,
            path="/",
            secure=settings.service.cookie_secure,
            httponly=True,
            samesite="strict",
        )

    @app.post("/api/auth/login")
    def login(body: Login, kb: KB, request: Request, response: Response):
        """Вход: новая сессия на `service.session_hours`.

        С `cookie=true` (браузер) сессия ставится в cookie `mb_session`, а в ответе только имя
        пользователя; `Origin` должен совпадать с сайтом (403). Без него — токен для
        `Authorization: Bearer`. 401 — неверные данные, 429 — много одновременных проверок
        пароля."""
        if body.cookie:
            check_same_origin(request)
        token = kb.store.login(
            body.username, body.password, ttl_hours=settings.service.session_hours
        )
        if body.cookie:
            set_session_cookie(response, token)
            return {"username": body.username.strip()}
        return {"token": token, "token_type": "bearer"}

    @app.post("/api/auth/logout")
    def logout(kb: KB, user: User, request: Request, response: Response):
        """Отзывает текущую сессию и стирает cookie."""
        kb.store.logout(session_token(request))
        response.delete_cookie(
            SESSION_COOKIE,
            path="/",
            secure=settings.service.cookie_secure,
            httponly=True,
            samesite="strict",
        )
        return {"status": "ok"}

    @app.post("/api/auth/password")
    def change_password(
        body: PasswordChange, kb: KB, user: User, request: Request, response: Response
    ):
        """Смена своего пароля: все сессии пользователя отзываются; браузер, из которого
        сменили пароль, сразу получает новую cookie-сессию. 401 — неверный текущий пароль,
        400 — новый короче 12 символов, 429 — заняты слоты argon2."""
        kb.store.change_password(user["id"], body.old_password, body.new_password)
        if request.state.auth == "cookie":
            token = kb.store.login_user_id(user["id"], settings.service.session_hours)
            set_session_cookie(response, token)
        return {"status": "ok"}

    @app.get("/api/auth/me")
    def me(user: User):
        """Текущий пользователь."""
        return {key: user[key] for key in ("id", "username")}

    @app.get("/api/documents")
    async def documents(kb: KB, user: User, limit: Limit = 50, offset: Offset = 0):
        """Страница документов корпуса."""
        return await kb.documents(limit=limit, offset=offset)

    @app.post("/api/documents", status_code=202)
    async def upload(
        request: Request,
        kb: KB,
        user: User,
        filename: Annotated[str, Query(min_length=1, max_length=255)],
    ):
        """Загрузка документа: сырое тело запроса — содержимое файла, имя — в `?filename=`.

        Отвечает 202 с задачей `ingest`; сам документ обработает worker. 413 — тело больше
        `service.max_upload_mb` (по `Content-Length` сразу, иначе по мере чтения); 422 —
        неверное имя, тип, `Content-Length` или содержимое; 429 — лимит токенов или очередь
        переполнена. Временный файл удаляется в любом случае (после успешной постановки его
        уже перенесли на место)."""
        kb.upload_suffix(filename)  # имя и тип — до чтения тела
        maximum = settings.service.max_upload_mb * 1024 * 1024
        length = request.headers.get("content-length")
        if length is not None:
            try:
                length_value = int(length)
            except ValueError as exc:
                raise ServiceError(422, "Неверная длина содержимого") from exc
            if length_value < 0:
                raise ServiceError(422, "Неверная длина содержимого")
            if length_value > maximum:
                raise ServiceError(413, "Загрузка слишком большая")
        # Поток сразу во временный файл с подсчётом sha256: тело не копится в памяти, а
        # запись на диск идёт в пуле потоков, чтобы не блокировать цикл сервера.
        handle, name = tempfile.mkstemp(dir=kb.staging_dir(), prefix=".upload-")
        staged = FilePath(name)
        try:
            digest, size = hashlib.sha256(), 0
            with os.fdopen(handle, "wb") as out:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > maximum:
                        raise ServiceError(413, "Загрузка слишком большая")
                    digest.update(chunk)
                    await run_in_threadpool(out.write, chunk)
            return await run_in_threadpool(kb.upload_file, filename, staged, digest.hexdigest())
        finally:
            staged.unlink(missing_ok=True)

    @app.get("/api/documents/{doc_id}/content")
    def source(doc_id: str, kb: KB, user: User):
        """Скачивание загруженного исходника вложением под исходным именем; 404 — нет."""
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

    @app.get("/api/documents/{doc_id}")
    async def document(doc_id: str, kb: KB, user: User):
        """Карточка документа: стадии, ошибки, число чанков, сущности; 404 — нет."""
        return await kb.document(doc_id)

    @app.get("/api/documents/{doc_id}/chunks")
    async def document_chunks(
        doc_id: str, kb: KB, user: User, limit: Limit = 50, offset: Offset = 0
    ):
        """Страница чанков документа по порядку и их общее число; 404 — нет документа."""
        return await kb.document_chunks(doc_id, limit=limit, offset=offset)

    @app.get("/api/chunks/{chunk_id}")
    async def chunk(chunk_id: str, kb: KB, user: User):
        """Полный текст фрагмента для цитаты; 404 — нет (например, документ удалён)."""
        return await kb.chunk(chunk_id)

    @app.delete("/api/documents/{doc_id}")
    async def delete_document(doc_id: str, kb: KB, user: User):
        """Ставит удаление документа из корпуса (задача `remove`); 404 — документ неизвестен."""
        return await kb.remove_document(doc_id)

    @app.get("/api/jobs")
    def jobs(kb: KB, user: User, limit: Limit = 50, offset: Offset = 0):
        """Страница задач очереди, новые первыми."""
        return kb.jobs(limit=limit, offset=offset)

    @app.post("/api/jobs/{job_id}/retry")
    def retry_job(job_id: Annotated[int, Path(ge=1, le=9223372036854775807)], kb: KB, user: User):
        """Повторяет упавшую задачу; 404 — нет задачи. `le` — предел BIGINT в PostgreSQL."""
        return kb.retry_job(job_id)

    @app.post("/api/search")
    async def search(body: Search, kb: KB, user: User):
        """Поиск фрагментов без генерации ответа."""
        return await kb.search(body.question, mode=body.mode, k=body.k)

    @app.post("/api/ask")
    async def ask(body: Question, kb: KB, user: User):
        """Ответ на вопрос с источниками (без истории диалога)."""
        return await kb.ask(body.question, mode=body.mode)

    @app.post("/api/conversations", status_code=201)
    def create_conversation(body: Conversation, kb: KB, user: User):
        """Создаёт диалог текущего пользователя (201)."""
        return kb.store.create_conversation(user["id"], body.title)

    @app.get("/api/conversations")
    def conversations(kb: KB, user: User, limit: Limit = 50, offset: Offset = 0):
        """Страница диалогов текущего пользователя, новые первыми."""
        return kb.store.conversations(user["id"], limit=limit, offset=offset)

    @app.patch("/api/conversations/{conversation_id}")
    def rename_conversation(conversation_id: str, body: Conversation, kb: KB, user: User):
        """Переименовывает диалог; 404 — чужой или нет."""
        return kb.store.rename_conversation(user["id"], conversation_id, body.title)

    @app.delete("/api/conversations/{conversation_id}")
    def delete_conversation(conversation_id: str, kb: KB, user: User):
        """Удаляет диалог со всеми сообщениями; 404 — чужой или нет."""
        kb.store.delete_conversation(user["id"], conversation_id)
        return {"status": "ok"}

    @app.get("/api/conversations/{conversation_id}/messages")
    def history(conversation_id: str, kb: KB, user: User, limit: Limit = 100, offset: Offset = 0):
        """Сообщения диалога с пометкой доступности источников; 404 — чужой или нет."""
        return kb.history(user["id"], conversation_id, limit=limit, offset=offset)

    @app.post("/api/conversations/{conversation_id}/messages")
    async def send_message(conversation_id: str, body: Question, kb: KB, user: User):
        """Вопрос в диалоге с учётом истории; возвращает сообщение ассистента."""
        return await kb.send_message(user["id"], conversation_id, body.question, mode=body.mode)

    @app.get("/api/wiki")
    async def wiki_pages(
        kb: KB,
        user: User,
        limit: Limit = 50,
        offset: Offset = 0,
        q: Annotated[str | None, Query(max_length=200)] = None,
    ):
        """Страница списка страниц wiki; `q` — поиск по заголовку, slug и псевдонимам."""
        return await kb.wiki_pages(limit=limit, offset=offset, q=q)

    @app.get("/api/wiki/by-slug/{slug}")
    async def wiki_page_by_slug(slug: str, kb: KB, user: User):
        """Страница wiki по slug (для ссылок `[[slug|текст]]`); 404 — нет."""
        return await kb.wiki_page_by_slug(slug)

    @app.get("/api/wiki/{page_id}")
    async def wiki_page(page_id: str, kb: KB, user: User):
        """Markdown страницы wiki; 404 — нет или устарела."""
        return await kb.wiki_page(page_id)

    @app.get("/api/graph/search")
    async def graph_search(
        kb: KB,
        user: User,
        q: Annotated[str, Query(min_length=1, max_length=1000)],
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ):
        """Полнотекстовый поиск сущностей графа по `q`."""
        return await kb.graph_search(q, limit=limit)

    @app.get("/api/graph/analysis")
    async def graph_analysis(kb: KB, user: User):
        """Состояние анализа графа (PageRank + Leiden) и топ сущностей."""
        return await kb.graph_analysis()

    @app.get("/api/graph/{entity_id}")
    async def entity(entity_id: str, kb: KB, user: User):
        """Карточка сущности с PageRank/сообществом и ссылкой на wiki; 404 — нет."""
        return await kb.entity(entity_id)

    @app.get("/api/graph/{entity_id}/neighbors")
    async def graph_neighbors(
        entity_id: str,
        kb: KB,
        user: User,
        depth: Annotated[int, Query(ge=1, le=3)] = 1,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ):
        """Окрестность сущности глубиной 1–3; 404 — нет сущности."""
        return await kb.graph_neighbors(entity_id, depth=depth, limit=limit)

    @app.get("/api/status/checks")
    def service_checks(kb: KB, user: User):
        """Быстрые проверки баз и серверов моделей; 429 — проверка уже идёт."""
        return kb.service_checks()

    @app.get("/api/status")
    def status(kb: KB, user: User):
        """Состояние очереди, worker'а и расход LLM."""
        return kb.status()

    return app
