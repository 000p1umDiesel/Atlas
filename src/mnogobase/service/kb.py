"""Личная база знаний: операции HTTP API поверх одного долгоживущего `App`.

Писатель корпуса один — worker. API только ставит задачи в `JobQueue` (загрузка,
удаление) и читает без блокировок, как CLI `ask`.

Async-клиенты `App` (LLM, семафор конкурентности) привязаны к одному event loop,
поэтому всё, что касается `App`, выполняется в выделенном потоке `CoreLoop`, а не
в цикле веб-сервера и не в новом цикле на каждый запрос.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from neo4j.exceptions import ServiceUnavailable, SessionExpired
from sqlalchemy.exc import DBAPIError

from mnogobase.app import App, build_app
from mnogobase.background.queue import JobQueue
from mnogobase.background.uploads import Uploads
from mnogobase.core.config import Settings
from mnogobase.core.database import DatabaseError, database_target
from mnogobase.core.log import get_logger
from mnogobase.core.models import Answer
from mnogobase.ingest.pipeline import EmbedderMismatchError
from mnogobase.llm.templates import render_messages
from mnogobase.retrieval import Mode, build_retrievers
from mnogobase.retrieval.answer import NO_SOURCES, Answerer
from mnogobase.retrieval.compare import run_mode
from mnogobase.retrieval.translate import QueryTranslator
from mnogobase.service.errors import ServiceError, check_pagination
from mnogobase.service.observability import UsageStore
from mnogobase.service.store import ServiceStore

_DOC_ID = re.compile(r"[0-9a-f]{16}")
# Обрыв соединения с базой: App пересоздаётся при следующем обращении.
_CONNECTION_ERRORS = (DatabaseError, DBAPIError, ServiceUnavailable, SessionExpired)
HEARTBEAT_STALE_S = 120.0


class CoreLoop:
    """Поток с собственным event loop, в котором живёт `App`."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="mnogobase-core", daemon=True
        )
        self._thread.start()

    async def run[T](self, coro: Awaitable[T]) -> T:
        # Отмена запроса не прерывает операцию ядра: она дорабатывает в своём цикле.
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, self._loop))

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)
        if not self._thread.is_alive():
            self._loop.close()


def _mode(mode: str) -> None:
    if mode not in {m.value for m in Mode}:
        raise ServiceError(422, "Unknown retrieval mode")


def _question(question: str) -> str:
    if not question.strip() or len(question) > 20000:
        raise ServiceError(422, "Question must contain 1–20000 characters")
    return question.strip()


def _job(job) -> dict:
    data = asdict(job)
    # Пути, fingerprints и JSON внутренних операций не являются публичным контрактом.
    for field in ("path", "fingerprint", "payload_json"):
        data.pop(field, None)
    return data


def _source_url(doc_id: str) -> str:
    return f"/api/documents/{doc_id}/content"


class KnowledgeBase:
    def __init__(
        self,
        settings: Settings,
        *,
        app_factory: Callable[[Settings], App] = build_app,
        store: ServiceStore | None = None,
        usage: UsageStore | None = None,
    ):
        self.settings = settings
        target = database_target(settings)
        self.store = store or ServiceStore(target)
        self.usage = usage or UsageStore(target)
        self.uploads = Uploads(settings)
        with self._queue():
            pass  # создаёт таблицы очереди до первого запроса
        self._factory = app_factory
        self._app: App | None = None
        self._core = CoreLoop()
        self._asks = asyncio.Semaphore(settings.service.max_concurrent_asks)
        self._log = get_logger(__name__)

    async def close(self) -> None:
        if self._app is not None:
            app, self._app = self._app, None
            await self._core.run(app.aclose())
        self._core.close()

    # ---- App в потоке ядра ----
    def _ensure_app(self) -> App:
        if self._app is None:
            app = self._factory(self.settings)
            try:
                app.pipeline.prepare(resume=False)
            except BaseException:
                app.close()
                raise
            self._app = app
        return self._app

    async def _drop_app(self) -> None:
        if self._app is not None:
            app, self._app = self._app, None
            try:
                await app.aclose()
            except Exception as exc:
                self._log.warning("app_close_failed", error_type=type(exc).__name__)

    async def _with_app[T](self, operation: Callable[[App], Awaitable[T]], failure: str) -> T:
        async def run() -> T:
            try:
                return await operation(self._ensure_app())
            except ServiceError:
                raise
            except EmbedderMismatchError:
                raise ServiceError(409, "Index needs `mnogobase reindex`") from None
            except _CONNECTION_ERRORS as exc:
                self._log.warning("core_connection_lost", error_type=type(exc).__name__)
                await self._drop_app()
                raise ServiceError(503, failure) from None
            except Exception as exc:
                # Текст исключения провайдера может содержать секреты: в лог только тип.
                self._log.warning("core_operation_failed", error_type=type(exc).__name__)
                raise ServiceError(503, failure) from None

        return await self._core.run(run())

    @contextmanager
    def _queue(self):
        queue = JobQueue(database_target(self.settings))
        try:
            yield queue
        finally:
            queue.close()

    def _queue_has_room(self, queue: JobQueue) -> None:
        counts = queue.counts()
        if (
            sum(counts.get(s, 0) for s in ("pending", "running"))
            >= self.settings.service.max_pending_jobs
        ):
            raise ServiceError(429, "Processing queue is full")

    def check_budget(self, now: float | None = None) -> None:
        """429, если за сутки (UTC) израсходован `service.daily_token_limit`: защита от
        случайного цикла или украденной сессии."""
        limit = self.settings.service.daily_token_limit
        if limit is None:
            return
        if self.usage.tokens_since(_day_start(time.time() if now is None else now)) >= limit:
            raise ServiceError(429, "Daily LLM token limit reached")

    # ---- документы и задачи (только очередь; в корпус пишет worker) ----
    def upload_suffix(self, filename: str) -> str:
        """Проверяет имя и тип загружаемого файла (до чтения тела запроса); вернёт суффикс."""
        if (
            not filename
            or filename in (".", "..")
            or any(c in filename for c in "/\\\x00")
            or len(filename) > 255
        ):
            raise ServiceError(422, "Invalid filename")
        suffix = Path(filename).suffix.lower()
        if suffix.lstrip(".") not in {
            x.lower().lstrip(".") for x in self.settings.parsing.extensions
        }:
            raise ServiceError(422, "Unsupported document type")
        return suffix

    def staging_dir(self) -> Path:
        """Каталог для временных файлов загрузки: рядом с итоговыми, чтобы перенос был
        атомарным `rename`."""
        self.uploads.root.mkdir(parents=True, exist_ok=True)
        return self.uploads.root

    def upload_file(self, filename: str, staged: Path, sha256: str) -> dict:
        """Ставит в очередь уже записанный во временный файл `staged` документ. Файл
        переносится на место загрузки; если он не нужен (дубликат), его удаляет вызывающий."""
        suffix = self.upload_suffix(filename)
        self.check_budget()
        size = staged.stat().st_size
        if not size:
            raise ServiceError(422, "Empty document")
        if size > self.settings.service.max_upload_mb * 1024 * 1024:
            raise ServiceError(413, "Document is too large")
        _check_signature(staged, suffix)
        doc_id = sha256[:16]
        previous = self.uploads.get(doc_id)
        path = Path(previous["path"]) if previous else self.uploads.root / (doc_id + suffix)
        try:
            self.uploads.contained(path, must_exist=False)
        except FileNotFoundError:
            raise ServiceError(409, "Upload storage is inconsistent") from None
        # Метаданные до постановки в очередь: повтор после прерывания их восстановит.
        self.uploads.add(doc_id, filename, path)
        with self._queue() as queue:
            resolved = str(path.resolve())
            existing = next(
                (
                    j
                    for j in queue.jobs(100000)
                    if j.path == resolved
                    and j.fingerprint == doc_id
                    and j.status not in ("failed", "superseded")
                ),
                None,
            )
            if existing is not None and queue.is_current(existing) and path.is_file():
                return _job(existing)
            self._queue_has_room(queue)
            path.parent.mkdir(parents=True, exist_ok=True)
            staged.replace(path)
            job_id = queue.enqueue(path, doc_id, force=True)
            return _job(queue.get(job_id))

    def _upload_path(self, doc_id: str) -> Path:
        if not _DOC_ID.fullmatch(doc_id):
            raise ServiceError(404, "Source unavailable")
        row = self.uploads.get(doc_id)
        if row is None:
            raise ServiceError(404, "Source unavailable")
        try:
            return self.uploads.contained(Path(row["path"]))
        except FileNotFoundError:
            raise ServiceError(404, "Source unavailable") from None

    def read_source(self, doc_id: str) -> tuple[bytes, str]:
        """Согласованный снимок исходника; удаление не прерывает передачу ответа."""
        path = self._upload_path(doc_id)
        limit = self.settings.service.max_upload_mb * 1024 * 1024
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ServiceError(413, "Document is too large")
        return data, self.uploads.get(doc_id)["name"]

    async def documents(self, limit: int = 50, offset: int = 0) -> list[dict]:
        """Все документы корпуса: загруженные через API и добавленные из CLI или папки."""
        check_pagination(limit, offset)

        async def run(app: App) -> list[dict]:
            out = []
            for row in app.registry.files()[offset : offset + limit]:
                upload = self.uploads.get(row.doc_id)
                uploaded = upload is not None and upload["path"] == row.path
                out.append(
                    {
                        "id": row.doc_id,
                        "name": upload["name"] if uploaded else Path(row.path).name,
                        "status": row.status,
                        "uploaded": uploaded,
                        "url": _source_url(row.doc_id) if uploaded else None,
                    }
                )
            return out

        return await self._with_app(run, "Document list unavailable")

    def jobs(self, limit: int = 50, offset: int = 0) -> list[dict]:
        check_pagination(limit, offset)
        with self._queue() as queue:
            return [_job(j) for j in queue.jobs(limit, offset)]

    def retry_job(self, job_id: int) -> dict:
        with self._queue() as queue:
            if queue.get(job_id) is None:
                raise ServiceError(404, "Job not found")
            queue.retry(job_id)
            return _job(queue.get(job_id))

    async def remove_document(self, doc_id: str) -> dict:
        if not _DOC_ID.fullmatch(doc_id):
            raise ServiceError(404, "Document not found")

        async def paths(app: App) -> list[str]:
            return app.registry.paths_for_doc(doc_id)

        known = await self._with_app(paths, "Document removal unavailable")
        upload = self.uploads.get(doc_id)
        if upload is not None and upload["path"] not in known:
            known.append(upload["path"])
        if not known:
            raise ServiceError(404, "Document not found")
        with self._queue() as queue:
            self._queue_has_room(queue)
            job_ids = [
                queue.enqueue(
                    Path(path),
                    "remove:" + doc_id,
                    force=True,
                    kind="remove",
                    payload_json=json.dumps({"doc_id": doc_id}),
                )
                for path in known
            ]
            return _job(queue.get(job_ids[-1]))

    # ---- чтение корпуса ----
    @staticmethod
    def _item(source: dict) -> dict:
        out = dict(source)
        if out.get("path"):
            out["path"] = Path(out["path"]).name
        doc_id = str(out.get("ref", "")).split(":", 1)[0]
        if _DOC_ID.fullmatch(doc_id) and out.get("kind") == "chunk":
            out["doc_id"] = doc_id
            out["url"] = _source_url(doc_id)
        elif out.get("kind") == "wiki":
            out["url"] = f"/api/wiki/{str(out['ref']).split('#')[0]}"
        return out

    def _items(self, sources: list[dict]) -> list[dict]:
        """Источники для ответа API. Загруженный файл лежит на диске как `<doc_id>.md`:
        вместо этого имени — то, под которым его загрузили."""
        items = [self._item(source) for source in sources]
        names: dict[str, str | None] = {}
        for item in items:
            doc_id = item.get("doc_id")
            if not doc_id or not item.get("path"):
                continue
            if doc_id not in names:
                upload = self.uploads.get(doc_id)
                names[doc_id] = upload["name"] if upload else None
            if names[doc_id]:
                item["path"] = names[doc_id]
        return items

    def _retrievers(self, app: App):
        return build_retrievers(
            app.vectors, app.graph, app.embedder, app.sparse, self.settings, app.reranker
        )

    async def search(self, question: str, mode: str = "rag", k: int = 8) -> list[dict]:
        question = _question(question)
        _mode(mode)
        if not 1 <= k <= 100:
            raise ServiceError(422, "Invalid search limit")
        await asyncio.to_thread(self.check_budget)

        async def run(app: App) -> list[dict]:
            if not app.registry.doc_ids():
                return []
            translator = QueryTranslator(app.llm) if self.settings.retrieval.translate else None
            alt = await translator.alt_queries(question) if translator else []
            items = self._retrievers(app)[mode].retrieve(question, k, alt)
            return self._items([x.model_dump() for x in items])

        async with self._asks:
            return await self._with_app(run, "Search service unavailable")

    async def _answer(
        self, app: App, question: str, mode: str, history: list[dict] | None = None
    ) -> dict:
        if not app.registry.doc_ids():
            return Answer(question=question, mode=mode, text=NO_SOURCES).model_dump(
                exclude={"contexts"}
            )
        query = question
        if history:
            prompt_history = json.dumps(
                [{"role": m["role"], "content": m["content"][:4000]} for m in history],
                ensure_ascii=False,
            )
            rewritten = (
                await app.llm.complete(
                    render_messages("chat_query", question=question, history=prompt_history),
                    task="query",
                )
            ).strip()
            if rewritten and len(rewritten) <= 4000:
                query = rewritten.splitlines()[0]
        settings = self.settings.retrieval
        answerer = Answerer(app.llm, settings.context_tokens, settings.answer_language)
        translator = QueryTranslator(app.llm) if settings.translate else None
        answer = await run_mode(
            query, mode, self._retrievers(app)[mode], answerer, settings.k, translator
        )
        out = answer.model_dump(exclude={"contexts"})
        out["question"], out["query"] = question, query
        out["sources"] = self._items(out["sources"])
        return out

    async def ask(self, question: str, mode: str = "rag") -> dict:
        question = _question(question)
        _mode(mode)
        await asyncio.to_thread(self.check_budget)
        async with self._asks:
            return await self._with_app(
                lambda app: self._answer(app, question, mode),
                "Model or knowledge service unavailable",
            )

    async def send_message(
        self, user_id: str, conversation_id: str, question: str, mode: str = "rag"
    ) -> dict:
        question = _question(question)
        _mode(mode)
        await asyncio.to_thread(self.check_budget)
        store = self.store
        await asyncio.to_thread(store.conversation, user_id, conversation_id)
        count = self.settings.service.history_messages
        messages = (
            await asyncio.to_thread(store.recent_messages, user_id, conversation_id, count)
            if count
            else []
        )
        history = [m for m in messages if m["status"] == "done"]
        await asyncio.to_thread(
            store.append_message, user_id, conversation_id, "user", question, mode=mode
        )
        try:
            async with self._asks:
                answer = await self._with_app(
                    lambda app: self._answer(app, question, mode, history),
                    "Model or knowledge service unavailable",
                )
        except ServiceError:
            # Не сохраняем текст исключения провайдера: он может содержать секреты.
            await asyncio.to_thread(
                store.append_message,
                user_id,
                conversation_id,
                "assistant",
                "Не удалось получить ответ. Повторите вопрос.",
                mode=mode,
                status="error",
            )
            raise
        return await asyncio.to_thread(
            store.append_message,
            user_id,
            conversation_id,
            "assistant",
            answer["text"],
            mode=mode,
            sources=answer.get("sources", []),
            query=answer.get("query", question),
        )

    def history(
        self, user_id: str, conversation_id: str, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        check_pagination(limit, offset)
        messages = self.store.messages(user_id, conversation_id, limit=limit, offset=offset)
        wiki_root = self.settings.wiki.dir.resolve()
        for message in messages:
            for source in message["sources"]:
                if "doc_id" in source:
                    try:
                        self._upload_path(source["doc_id"])
                    except ServiceError:
                        source["available"] = False
                    else:
                        source["available"] = True
                elif source.get("kind") == "wiki" and source.get("path"):
                    path = (self.settings.wiki.dir / "entities" / source["path"]).resolve()
                    source["available"] = path.is_relative_to(wiki_root) and path.is_file()
        return messages

    async def wiki_pages(self, limit: int = 50, offset: int = 0) -> list[dict]:
        check_pagination(limit, offset)

        async def run(app: App) -> list[dict]:
            return [
                {k: v for k, v in row.model_dump().items() if k != "path"}
                for row in app.graph.wiki_pages()[offset : offset + limit]
            ]

        return await self._with_app(run, "Wiki unavailable")

    async def wiki_page(self, page_id: str) -> dict:
        async def run(app: App) -> dict:
            page = app.graph.wiki_page(page_id)
            if page is None or page.stale:
                raise ServiceError(404, "Wiki page not found")
            root = self.settings.wiki.dir.resolve()
            path = (self.settings.wiki.dir / page.path).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ServiceError(404, "Wiki page unavailable")
            return {
                "id": page.page_id,
                "title": page.title,
                "slug": page.slug,
                "content": path.read_text(encoding="utf-8"),
            }

        return await self._with_app(run, "Wiki unavailable")

    async def graph_search(self, query: str, limit: int = 20) -> list[dict]:
        check_pagination(limit, 0)

        async def run(app: App) -> list[dict]:
            return [
                app.graph.get_entity(eid).model_dump()
                for eid, _ in app.graph.fulltext_entities(query, limit)
            ]

        return await self._with_app(run, "Graph unavailable")

    async def graph_neighbors(self, entity_id: str, depth: int = 1, limit: int = 50) -> dict:
        check_pagination(limit, 0)
        if not 1 <= depth <= 3:
            raise ServiceError(422, "Graph depth must be between 1 and 3")

        async def run(app: App) -> dict:
            entity = app.graph.get_entity(entity_id)
            if entity is None:
                raise ServiceError(404, "Entity not found")
            relations = app.graph.neighborhood([entity_id], depth, limit)
            ids = {entity_id} | {r.src_id for r in relations} | {r.dst_id for r in relations}
            nodes = [app.graph.get_entity(eid).model_dump() for eid in sorted(ids)]
            return {"nodes": nodes, "relations": [r.model_dump() for r in relations]}

        return await self._with_app(run, "Graph unavailable")

    # ---- состояние ----
    def status(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        with self._queue() as queue:
            counts = queue.counts()
            heartbeat = queue.last_heartbeat()
            running = [_job(j) for j in queue.jobs(1000) if j.status == "running"]
        day = _day_start(now)
        month = datetime.fromtimestamp(day, UTC).replace(day=1).timestamp()
        return {
            "queue": counts,
            "running": running,
            "worker": {
                "last_seen": heartbeat,
                "alive": heartbeat is not None and now - heartbeat < HEARTBEAT_STALE_S,
            },
            "llm_usage": {
                "today": self.usage.summary(since=day),
                "month": self.usage.summary(since=month),
            },
        }


def _day_start(now: float) -> float:
    return (
        datetime.fromtimestamp(now, UTC)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp()
    )


# Первые байты файлов с двоичным форматом: загрузка с чужим содержимым под знакомым
# расширением отклоняется до парсера. Текстовые форматы (md, txt, html, csv...) не проверяются.
_ZIP = (b"PK\x03\x04",)
_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    ".pdf": (b"%PDF-",),
    ".docx": _ZIP,
    ".pptx": _ZIP,
    ".xlsx": _ZIP,
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".tif": (b"II*\x00", b"MM\x00*"),
    ".tiff": (b"II*\x00", b"MM\x00*"),
    ".bmp": (b"BM",),
}


def _check_signature(path: Path, suffix: str) -> None:
    with path.open("rb") as stream:
        head = stream.read(16)
    if suffix == ".webp":
        valid = head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    else:
        valid = head.startswith(_SIGNATURES.get(suffix, (b"",)))
    if not valid:
        raise ServiceError(422, "File content does not match its type")
