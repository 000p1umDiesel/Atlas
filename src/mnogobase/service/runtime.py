"""Прикладные операции командной базы, общие для HTTP и операторского CLI."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict
from pathlib import Path

from filelock import Timeout

from mnogobase.app import build_app
from mnogobase.background.cli import JobProgress
from mnogobase.background.queue import JobQueue
from mnogobase.config import Settings
from mnogobase.database import (
    connect,
    database_lock,
    database_target,
    initialize,
    target_exists,
)
from mnogobase.llm.telemetry import usage_scope
from mnogobase.llm.templates import render_messages
from mnogobase.log import team_logging
from mnogobase.models import Answer
from mnogobase.progress import NullProgress
from mnogobase.registry import Registry, ingest_lock
from mnogobase.retrieval import Mode, build_retrievers
from mnogobase.retrieval.answer import NO_SOURCES, Answerer
from mnogobase.retrieval.compare import run_mode
from mnogobase.retrieval.translate import QueryTranslator
from mnogobase.service.async_ops import offload_core
from mnogobase.service.context import TeamContext, team_settings
from mnogobase.service.errors import ServiceError
from mnogobase.service.monitor import worker_heartbeat
from mnogobase.service.observability import ObservabilityStore
from mnogobase.service.store import ServiceStore
from mnogobase.stores.graph_store import GraphStore

_DOC_ID = re.compile(r"[0-9a-f]{16}")


class TeamService:
    def __init__(self, settings: Settings, *, app_factory=build_app, graph_factory=None):
        self.settings = settings.model_copy(deep=True)
        self.store = ServiceStore(database_target(self.settings, "service"))
        self.observability = ObservabilityStore(database_target(self.settings, "service"))
        self._factory = app_factory
        self._graph_factory = graph_factory or (
            GraphStore.from_settings if app_factory is build_app else None
        )
        self._next_team = 0

    def _check(self, ctx: TeamContext, *, admin: bool = False) -> TeamContext:
        return self.store.context(ctx.user_id, ctx.team_id, admin=admin)

    def _settings(self, team_id: str) -> Settings:
        settings = team_settings(self.settings, team_id)
        root = self.settings.service.root.resolve() / "teams" / team_id
        # Серверные IDs не позволяют traversal; не доверяем также подменённым symlinks.
        for path in (
            settings.data_dir,
            settings.wiki.dir,
            settings.logs_dir,
            settings.runs_dir,
            settings.data_dir.parent / "documents",
        ):
            if path.resolve() != root / path.name:
                raise ServiceError(409, "Invalid team storage")
        return settings

    @contextmanager
    def _locked(self, ctx: TeamContext, *, admin: bool = False):
        self._check(ctx, admin=admin)
        settings = self._settings(ctx.team_id)
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        lock = ingest_lock(settings.data_dir, target=database_target(settings))
        try:
            lock.acquire()
        except Timeout:
            raise ServiceError(409, "База команды обновляется; повторите запрос позже") from None
        try:
            self._check(ctx, admin=admin)
            with team_logging(ctx.team_id, settings.logs_dir):
                yield settings
        finally:
            lock.release()

    @contextmanager
    def _uploads(self, settings: Settings):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        db = connect(database_target(settings, "uploads"))
        try:
            initialize(
                db,
                "CREATE TABLE IF NOT EXISTS uploads(doc_id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL)",
                "uploads",
            )
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def _graph(self, settings: Settings):
        # Чтение Wiki/графа не создаёт парсер, BM25, LLM и embedding HTTP clients.
        if self._graph_factory is not None:
            graph = self._graph_factory(settings.neo4j)
            try:
                yield graph
            finally:
                graph.close()
        else:
            # Инъекция полного App сохраняется для изолированных тестов ядра.
            application = self._factory(settings)
            try:
                yield application.graph
            finally:
                application.close()

    @contextmanager
    def _queue(self, settings: Settings):
        queue = JobQueue(database_target(settings, "jobs"))
        try:
            yield queue
        finally:
            queue.close()

    @asynccontextmanager
    async def _app(self, settings: Settings, *, job_id: int | None = None):
        def record(event):
            self.observability.record_usage(event, self.settings.service.llm_rates.get(event.model))

        with (
            self._capacity(),
            team_logging(settings.neo4j.team_id, settings.logs_dir),
            usage_scope(settings.neo4j.team_id, record, job_id=job_id),
        ):
            application = self._factory(settings)
            try:
                yield application
            finally:
                if hasattr(application, "aclose"):
                    await application.aclose()
                else:
                    application.close()

    @contextmanager
    def _capacity(self):
        """Межпроцессный предел операций; отмена caller не освобождает слот worker."""
        root = self.settings.service.root
        root.mkdir(parents=True, exist_ok=True)
        for index in range(self.settings.service.max_concurrent_operations):
            lock = database_lock(
                self.settings, f"operation-{index}", root / f"operation-{index}.lock", service=True
            )
            try:
                lock.acquire()
            except Timeout:
                continue
            try:
                yield
            finally:
                lock.release()
            return
        raise ServiceError(429, "Model operation capacity is full; retry later")

    @staticmethod
    def _page(limit: int, offset: int) -> None:
        if not 1 <= limit <= 1000 or offset < 0:
            raise ServiceError(422, "Invalid pagination")

    @staticmethod
    def _mode(mode: str) -> None:
        if mode not in {m.value for m in Mode}:
            raise ServiceError(422, "Unknown retrieval mode")

    @staticmethod
    def _question(question: str) -> str:
        if not question.strip() or len(question) > 20000:
            raise ServiceError(422, "Question must contain 1–20000 characters")
        return question.strip()

    @staticmethod
    def _job(job) -> dict:
        data = asdict(job)
        # Пути, fingerprints и JSON внутренних операций не являются публичным контрактом.
        for field in ("path", "fingerprint", "payload_json"):
            data.pop(field, None)
        return data

    def upload(self, ctx: TeamContext, filename: str, content: bytes) -> dict:
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
        if not content:
            raise ServiceError(422, "Empty document")
        if len(content) > self.settings.service.max_upload_mb * 1024 * 1024:
            raise ServiceError(413, "Document is too large")
        doc_id = hashlib.sha256(content).hexdigest()[:16]
        with (
            self._locked(ctx) as settings,
            self._uploads(settings) as db,
            self._queue(settings) as q,
        ):
            previous = db.execute("SELECT * FROM uploads WHERE doc_id=?", (doc_id,)).fetchone()
            path = (
                Path(previous["path"])
                if previous
                else settings.data_dir.parent / "documents" / (doc_id + suffix)
            )
            self._contained_source(settings, path, must_exist=False)
            # Restore metadata even when a previous enqueue committed before the
            # uploads transaction (e.g. interruption during its commit).
            db.execute(
                "INSERT INTO uploads VALUES(?,?,?) ON CONFLICT(doc_id) DO NOTHING",
                (doc_id, filename, str(path.resolve())),
            )
            fingerprint = doc_id
            existing = next(
                (
                    j
                    for j in q.jobs(100000)
                    if j.path == str(path.resolve())
                    and j.fingerprint == fingerprint
                    and j.status not in ("failed", "superseded")
                ),
                None,
            )
            if existing is not None and q.is_current(existing) and path.is_file():
                return self._job(existing)
            counts = q.counts()
            if (
                sum(counts.get(s, 0) for s in ("pending", "running"))
                >= self.settings.service.max_pending_jobs
            ):
                raise ServiceError(429, "Team processing queue is full")
            path.parent.mkdir(parents=True, exist_ok=True)
            # Запись и постановка в очередь повторяемы после прерывания процесса.
            with tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=".upload-", delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(content)
            try:
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            job_id = q.enqueue(path, fingerprint, force=True)
            return self._job(q.get(job_id))

    def documents(self, ctx: TeamContext, limit: int = 50, offset: int = 0) -> list[dict]:
        self._page(limit, offset)
        with self._locked(ctx) as settings, self._uploads(settings) as db:
            rows = db.execute(
                "SELECT * FROM uploads ORDER BY doc_id LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
            registry = Registry(database_target(settings))
            try:
                out = []
                for row in rows:
                    entry = registry.get_file(row["path"])
                    out.append(
                        {
                            "id": row["doc_id"],
                            "name": row["name"],
                            "status": entry.status if entry else "pending",
                            "url": self._source_url(ctx.team_id, row["doc_id"]),
                        }
                    )
                return out
            finally:
                registry.close()

    @staticmethod
    def _source_url(team_id: str, doc_id: str) -> str:
        return f"/api/teams/{team_id}/documents/{doc_id}/content"

    @staticmethod
    def _contained_source(settings: Settings, path: Path, *, must_exist: bool = True) -> Path:
        root = (settings.data_dir.parent / "documents").resolve()
        resolved = path.resolve()
        if (
            path.is_symlink()
            or not resolved.is_relative_to(root)
            or (must_exist and not resolved.is_file())
        ):
            raise ServiceError(404, "Source unavailable")
        return resolved

    def _source(self, settings: Settings, doc_id: str) -> Path:
        if not _DOC_ID.fullmatch(doc_id):
            raise ServiceError(404, "Source unavailable")
        with self._uploads(settings) as db:
            row = db.execute("SELECT path FROM uploads WHERE doc_id=?", (doc_id,)).fetchone()
        if row is None:
            raise ServiceError(404, "Source unavailable")
        return self._contained_source(settings, Path(row["path"]))

    def source(self, ctx: TeamContext, doc_id: str) -> Path:
        with self._locked(ctx) as settings:
            return self._source(settings, doc_id)

    def read_source(self, ctx: TeamContext, doc_id: str) -> tuple[bytes, str]:
        """Согласованный снимок исходника; удаление не прерывает передачу ответа."""
        with self._locked(ctx) as settings:
            path = self._source(settings, doc_id)
            with path.open("rb") as stream:
                data = stream.read(settings.service.max_upload_mb * 1024 * 1024 + 1)
            if len(data) > settings.service.max_upload_mb * 1024 * 1024:
                raise ServiceError(413, "Document is too large")
            with self._uploads(settings) as db:
                name = db.execute("SELECT name FROM uploads WHERE doc_id=?", (doc_id,)).fetchone()[
                    0
                ]
            self._check(ctx)
            return data, name

    def jobs(self, ctx: TeamContext, limit: int = 50, offset: int = 0) -> list[dict]:
        self._page(limit, offset)
        self._check(ctx)
        with self._queue(self._settings(ctx.team_id)) as queue:
            return [self._job(j) for j in queue.jobs(limit, offset)]

    def retry_job(self, ctx: TeamContext, job_id: int) -> dict:
        self._check(ctx)
        with self._queue(self._settings(ctx.team_id)) as queue:
            job = queue.get(job_id)
            if job is None:
                raise ServiceError(404, "Job not found")
            if job.kind == "remove":
                self._check(ctx, admin=True)
            queue.retry(job_id)
            return self._job(queue.get(job_id))

    @offload_core
    async def remove_document(self, ctx: TeamContext, doc_id: str) -> dict:
        with self._locked(ctx, admin=True) as settings, self._queue(settings) as queue:
            path = self._source(settings, doc_id)
            counts = queue.counts()
            if (
                sum(counts.get(s, 0) for s in ("pending", "running"))
                >= self.settings.service.max_pending_jobs
            ):
                raise ServiceError(429, "Team processing queue is full")
            job_id = queue.enqueue(
                path,
                "remove:" + doc_id,
                force=True,
                kind="remove",
                payload_json=json.dumps({"doc_id": doc_id}),
            )
            return self._job(queue.get(job_id))

    @staticmethod
    def _has_sources(settings: Settings) -> bool:
        if not target_exists(database_target(settings)):
            return False
        registry = Registry(database_target(settings))
        try:
            return bool(registry.doc_ids())
        finally:
            registry.close()

    def _knowledge_ready(self, settings: Settings) -> None:
        # После неудачной записи lock отпущен, но три слоя ещё могут расходиться.
        with self._queue(settings) as queue:
            unfinished = queue.has_unfinished_writes()
        if unfinished:
            raise ServiceError(
                409, "База требует завершения обработки; проверьте задания и повторите ошибку"
            )

    @staticmethod
    def _retrievers(app, settings):
        return build_retrievers(
            app.vectors, app.graph, app.embedder, app.sparse, settings, app.reranker
        )

    @offload_core
    async def search(
        self, ctx: TeamContext, question: str, mode: str = "rag", k: int = 8
    ) -> list[dict]:
        question = self._question(question)
        self._mode(mode)
        if not 1 <= k <= 100:
            raise ServiceError(422, "Invalid search limit")
        with self._locked(ctx) as settings:
            self._knowledge_ready(settings)
            if not self._has_sources(settings):
                return []
            try:
                async with self._app(settings) as app:
                    app.pipeline.prepare(resume=False)
                    translator = QueryTranslator(app.llm) if settings.retrieval.translate else None
                    alt = await translator.alt_queries(question) if translator else []
                    items = self._retrievers(app, settings)[mode].retrieve(question, k, alt)
                    return [self._item(ctx, x.model_dump()) for x in items]
            except ServiceError:
                raise
            except Exception:
                raise ServiceError(503, "Search service unavailable") from None

    def _item(self, ctx: TeamContext, source: dict) -> dict:
        out = dict(source)
        if out.get("path"):
            out["path"] = Path(out["path"]).name
        doc_id = str(out.get("ref", "")).split(":", 1)[0]
        if _DOC_ID.fullmatch(doc_id) and out.get("kind") == "chunk":
            out["doc_id"] = doc_id
            out["url"] = self._source_url(ctx.team_id, doc_id)
        elif out.get("kind") == "wiki":
            out["url"] = f"/api/teams/{ctx.team_id}/wiki/{str(out['ref']).split('#')[0]}"
        return out

    async def _answer(
        self,
        ctx: TeamContext,
        settings: Settings,
        question: str,
        mode: str,
        history: list[dict] | None = None,
    ) -> dict:
        self._knowledge_ready(settings)
        if not self._has_sources(settings):
            return Answer(question=question, mode=mode, text=NO_SOURCES).model_dump(
                exclude={"contexts"}
            )
        async with self._app(settings) as app:
            app.pipeline.prepare(resume=False)
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
            answerer = Answerer(
                app.llm, settings.retrieval.context_tokens, settings.retrieval.answer_language
            )
            translator = QueryTranslator(app.llm) if settings.retrieval.translate else None
            answer = await run_mode(
                query,
                mode,
                self._retrievers(app, settings)[mode],
                answerer,
                settings.retrieval.k,
                translator,
            )
            out = answer.model_dump(exclude={"contexts"})
            out["question"], out["query"] = question, query
            out["sources"] = [self._item(ctx, s) for s in out["sources"]]
            return out

    @offload_core
    async def ask(self, ctx: TeamContext, question: str, mode: str = "rag") -> dict:
        question = self._question(question)
        self._mode(mode)
        with self._locked(ctx) as settings:
            try:
                result = await self._answer(ctx, settings, question, mode)
                self._check(ctx)
                return result
            except ServiceError:
                raise
            except Exception:
                raise ServiceError(503, "Model or knowledge service unavailable") from None

    @offload_core
    async def send_message(
        self, ctx: TeamContext, conversation_id: str, question: str, mode: str = "rag"
    ) -> dict:
        question = self._question(question)
        self._mode(mode)
        self.store.conversation(ctx, conversation_id)
        with self._locked(ctx) as settings:
            # Блокировка корпуса также сериализует сообщения и их историю внутри команды.
            self._knowledge_ready(settings)
            count = settings.service.history_messages
            messages = (
                self.store.recent_messages(ctx, conversation_id, limit=count) if count else []
            )
            history = [m for m in messages if m["status"] == "done"]
            self.store.append_message(ctx, conversation_id, "user", question, mode=mode)
            try:
                answer = await self._answer(ctx, settings, question, mode, history)
                self._check(ctx)
                return self.store.append_message(
                    ctx,
                    conversation_id,
                    "assistant",
                    answer["text"],
                    mode=mode,
                    sources=answer.get("sources", []),
                    query=answer.get("query", question),
                )
            except ServiceError:
                raise
            except Exception:
                # Не сохраняем текст provider exception: он может содержать секреты.
                self.store.append_message(
                    ctx,
                    conversation_id,
                    "assistant",
                    "Не удалось получить ответ. Повторите вопрос.",
                    mode=mode,
                    status="error",
                )
                raise ServiceError(503, "Model or knowledge service unavailable") from None

    def history(
        self, ctx: TeamContext, conversation_id: str, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        self._page(limit, offset)
        with self._locked(ctx) as settings:
            messages = self.store.messages(ctx, conversation_id, limit=limit, offset=offset)
            for message in messages:
                for source in message["sources"]:
                    if "doc_id" in source:
                        try:
                            self._source(settings, source["doc_id"])
                        except ServiceError:
                            source["available"] = False
                        else:
                            source["available"] = True
                    elif source.get("kind") == "wiki":
                        page_id = str(source.get("ref", "")).split("#", 1)[0]
                        with self._graph(settings) as graph:
                            page = graph.wiki_page(page_id)
                        path = settings.wiki.dir / page.path if page else None
                        source["available"] = bool(
                            path
                            and path.resolve().is_relative_to(settings.wiki.dir.resolve())
                            and path.is_file()
                        )
            return messages

    def wiki_pages(self, ctx: TeamContext, limit: int = 50, offset: int = 0) -> list[dict]:
        self._page(limit, offset)
        with self._locked(ctx) as settings:
            self._knowledge_ready(settings)
            if not self._has_sources(settings):
                return []
            with self._graph(settings) as graph:
                return [
                    {k: v for k, v in row.model_dump().items() if k != "path"}
                    for row in graph.wiki_pages()[offset : offset + limit]
                ]

    def wiki_page(self, ctx: TeamContext, page_id: str) -> dict:
        with self._locked(ctx) as settings, self._graph(settings) as graph:
            self._knowledge_ready(settings)
            page = graph.wiki_page(page_id)
            if page is None:
                raise ServiceError(404, "Wiki page not found")
            path = settings.wiki.dir / page.path
            if not path.resolve().is_relative_to(settings.wiki.dir.resolve()) or not path.is_file():
                raise ServiceError(404, "Wiki page unavailable")
            return {
                "id": page.page_id,
                "title": page.title,
                "slug": page.slug,
                "content": path.read_text(encoding="utf-8"),
            }

    def graph_search(self, ctx: TeamContext, query: str, limit: int = 20) -> list[dict]:
        self._page(limit, 0)
        with self._locked(ctx) as settings:
            self._knowledge_ready(settings)
            if not self._has_sources(settings):
                return []
            with self._graph(settings) as graph:
                return [
                    graph.get_entity(eid).model_dump()
                    for eid, _ in graph.fulltext_entities(query, limit)
                ]

    def graph_neighbors(
        self, ctx: TeamContext, entity_id: str, depth: int = 1, limit: int = 50
    ) -> dict:
        self._page(limit, 0)
        if not 1 <= depth <= 3:
            raise ServiceError(422, "Graph depth must be between 1 and 3")
        with self._locked(ctx) as settings, self._graph(settings) as graph:
            self._knowledge_ready(settings)
            entity = graph.get_entity(entity_id)
            if entity is None:
                raise ServiceError(404, "Entity not found")
            relations = graph.neighborhood([entity_id], depth, limit)
            ids = {entity_id} | {r.src_id for r in relations} | {r.dst_id for r in relations}
            nodes = [graph.get_entity(eid).model_dump() for eid in sorted(ids)]
            return {"nodes": nodes, "relations": [r.model_dump() for r in relations]}

    @offload_core
    async def work_once(self) -> int:
        root = self.settings.service.root
        root.mkdir(parents=True, exist_ok=True)
        lock = database_lock(self.settings, "worker", root / "worker.lock", service=True)
        try:
            lock.acquire()
        except Timeout:
            raise ServiceError(409, "Another service worker is running") from None
        try:
            with worker_heartbeat(self.observability, self.settings.service.monitor.heartbeat_s):
                return await self._work_teams()
        finally:
            lock.release()

    async def _work_teams(self) -> int:
        teams = self.store.teams()
        if not teams:
            return 0
        start = self._next_team % len(teams)
        teams = teams[start:] + teams[:start]
        processed = 0
        for team in teams:
            settings = self._settings(team["id"])
            settings.data_dir.mkdir(parents=True, exist_ok=True)
            corpus_lock = ingest_lock(settings.data_dir, target=database_target(settings))
            local_worker = database_lock(settings, "worker", settings.data_dir / "worker.lock")
            try:
                local_worker.acquire()
                corpus_lock.acquire()
            except Timeout:
                if local_worker.is_locked:
                    local_worker.release()
                continue
            try:
                with self._queue(settings) as queue:
                    queue.recover()
                    job = queue.claim()
                    if job is None:
                        continue
                    processed += 1
                    try:
                        if not queue.is_current(job):
                            queue.supersede(job.id)
                            continue
                        path = self._contained_source(
                            settings, Path(job.path), must_exist=job.kind != "remove"
                        )
                        async with self._app(settings, job_id=job.id) as app:
                            progress = JobProgress(queue, job.id, NullProgress())
                            if job.kind == "remove":
                                doc_id = json.loads(job.payload_json)["doc_id"]
                                report = await app.pipeline.remove([path], progress=progress)
                                if report.wiki and report.wiki.failed:
                                    raise RuntimeError("Wiki rebuild failed")
                                path.unlink(missing_ok=True)
                                with self._uploads(settings) as db:
                                    db.execute("DELETE FROM uploads WHERE doc_id=?", (doc_id,))
                            elif job.kind == "ingest":
                                if (
                                    hashlib.sha256(path.read_bytes()).hexdigest()[:16]
                                    != job.fingerprint
                                ):
                                    queue.supersede(job.id)
                                    continue
                                report = await app.pipeline.ingest(
                                    [path], retry_failed=True, progress=progress
                                )
                                if report.failed or (report.wiki and report.wiki.failed):
                                    raise RuntimeError("Document processing failed")
                            else:
                                raise ValueError("Unsupported job kind")
                        queue.finish(job.id)
                    except Exception as exc:
                        queue.fail(job.id, type(exc).__name__, max_attempts=3, delay=1)
            finally:
                corpus_lock.release()
                local_worker.release()
        self._next_team = (start + 1) % len(teams)
        return processed
