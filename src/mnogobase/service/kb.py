"""Личная база знаний: операции HTTP API поверх одного долгоживущего `App`.

`KnowledgeBase` — фасад, который `api/app.py` вызывает из обработчиков; на процесс API
одна база (один проект, одна схема PostgreSQL). Ошибки наружу — только `ServiceError`.

Один писатель. Корпус (Qdrant, Neo4j, wiki, registry) меняет только worker (или
CLI-команда под `ingest_lock`). API только ставит задачи в `JobQueue` (загрузка, удаление)
и читает без блокировок, как CLI `ask`.

Поток ядра `CoreLoop`. Async-клиенты `App` (LLM-клиент и его семафор конкурентности)
привязаны к event loop, в котором созданы, поэтому всё, что касается `App`, выполняется в
одном выделенном потоке со своим циклом, а не в цикле веб-сервера и не в новом цикле на
каждый запрос. Обработчик ждёт результат через `asyncio.wrap_future`.

Ленивый `App`. `App` создаётся при первом обращении (`_ensure_app`), а не при старте:
API поднимается, даже если модели или базы ещё не готовы. При создании проверяется
сигнатура индекса (`pipeline.prepare`). После обрыва соединения с базой `App` закрывается
и при следующем запросе создаётся заново.

`_with_app` — единственный вход в ядро и отображение ошибок: `ServiceError` проходит как
есть, `EmbedderMismatchError` → 409 (нужен `mnogobase reindex`), обрыв соединения
(PostgreSQL, Neo4j) → 503 с пересозданием `App`, любая другая ошибка → 503. В лог пишется
только тип исключения: текст ошибки провайдера может содержать ключи.

Ограничения нагрузки: `asyncio.Semaphore(service.max_concurrent_asks)` ограничивает
одновременные `ask` / `search` / сообщения чата (лишние ждут в очереди), суточный лимит
токенов `service.daily_token_limit` — 429, переполненная очередь задач
(`service.max_pending_jobs`) — 429.

Загрузка. Обработчик API потоково пишет тело во временный файл в каталоге загрузок,
считая sha256, и вызывает `upload_file`: проверка имени, расширения, размера и сигнатуры
формата, запись `uploads` (имя ↔ `doc_id`), затем `JobQueue.enqueue(force=True)`, который
под блокировкой пути переносит файл на место `<doc_id><расширение>` и ставит задачу
`ingest`. Повторная загрузка того же содержимого возвращает уже существующую задачу,
если та не упала и не заменена.

Удаление. `remove_document` находит все пути документа (в registry и в `uploads`) и ставит
на каждый задачу `remove` с версией `remove:<doc_id>`. Worker забывает источник в корпусе
и удаляет файл, только если он из каталога загрузок; файлы владельца остаются на месте.
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
from mnogobase.service.errors import MAX_QUESTION_CHARS, ServiceError, check_pagination
from mnogobase.service.observability import UsageStore
from mnogobase.service.store import ServiceStore

_DOC_ID = re.compile(r"[0-9a-f]{16}")
_CHUNK_ID = re.compile(r"[0-9a-f]{16}:\d{5}")
# Обрыв соединения с PostgreSQL или Neo4j: App пересоздаётся при следующем обращении.
_CONNECTION_ERRORS = (DatabaseError, DBAPIError, ServiceUnavailable, SessionExpired)
# Worker считается живым, если отмечался в очереди не позже этого числа секунд назад.
HEARTBEAT_STALE_S = 120.0


class CoreLoop:
    """Поток-демон `mnogobase-core` с собственным event loop, в котором живёт `App`.

    Все корутины, работающие с `App`, запускаются здесь через `run`, поэтому async-клиенты
    `App` всегда видят один и тот же цикл. Синхронные вызовы внутри корутин (Qdrant, Neo4j,
    PostgreSQL) блокируют этот цикл: параллельно идут только ожидания async-клиентов (LLM),
    остальная работа ядра выполняется по очереди."""

    def __init__(self) -> None:
        """Создаёт цикл и сразу запускает поток."""
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="mnogobase-core", daemon=True
        )
        self._thread.start()

    async def run[T](self, coro: Awaitable[T]) -> T:
        """Выполняет корутину в цикле ядра и ждёт её результат из текущего цикла.

        Отмена ожидающего (клиент закрыл соединение) не прерывает операцию ядра: она
        дорабатывает в своём цикле, и `App` не остаётся в промежуточном состоянии."""
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, self._loop))

    def close(self) -> None:
        """Останавливает цикл и ждёт поток до 10 с; цикл закрывается, только если поток
        действительно завершился (иначе закрытие работающего цикла упало бы)."""
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)
        if not self._thread.is_alive():
            self._loop.close()


def _mode(mode: str) -> None:
    """422, если `mode` — не известный режим поиска (`rag` / `wiki` / `graph` / `all`)."""
    if mode not in {m.value for m in Mode}:
        raise ServiceError(422, "Неизвестный режим поиска")


def _question(question: str) -> str:
    """Вопрос без пробелов по краям; 422, если он пустой или длиннее `MAX_QUESTION_CHARS`."""
    if not question.strip() or len(question) > MAX_QUESTION_CHARS:
        raise ServiceError(422, f"Вопрос должен содержать от 1 до {MAX_QUESTION_CHARS} символов")
    return question.strip()


def _job(job, names: dict[str, str] | None = None) -> dict:
    """Задача очереди в виде ответа API, без внутренних полей.

    Добавляются `doc_id` (из версии `<doc_id>` или `remove:<doc_id>`, иначе `None`) и `name`:
    имя загрузки из `names` (`doc_id` → имя), иначе имя файла без каталога; у задачи анализа
    графа — «Анализ графа»."""
    data = asdict(job)
    doc_id = job.fingerprint.removeprefix("remove:")
    data["doc_id"] = doc_id if _DOC_ID.fullmatch(doc_id) else None
    if job.kind == "analyze":
        data["name"] = "Анализ графа"
    else:
        data["name"] = (names or {}).get(data["doc_id"] or "") or Path(job.path).name
    # Пути, fingerprints и JSON внутренних операций не являются публичным контрактом.
    for field in ("path", "fingerprint", "payload_json"):
        data.pop(field, None)
    return data


def _meta(answer: dict) -> dict:
    """Метрики ответа для сообщения диалога: полное время, токены ответа и переводы запроса."""
    return {
        "latency_ms": int(answer.get("latency_ms", 0)),
        "tokens_in": int(answer.get("tokens_in", 0)),
        "tokens_out": int(answer.get("tokens_out", 0)),
        "alt_queries": list(answer.get("alt_queries", [])),
    }


def _source_url(doc_id: str) -> str:
    """URL API для скачивания загруженного исходника."""
    return f"/api/documents/{doc_id}/content"


class KnowledgeBase:
    """Операции HTTP API над одной базой знаний (см. описание модуля).

    Синхронные методы (`jobs`, `status`, `upload_file`, ...) FastAPI вызывает в пуле потоков;
    асинхронные работают с `App` через `CoreLoop`. Очередь открывается заново на каждый
    вызов (`_queue`): `JobQueue` держит одно соединение и не потокобезопасен."""

    def __init__(
        self,
        settings: Settings,
        *,
        app_factory: Callable[[Settings], App] = build_app,
        store: ServiceStore | None = None,
        usage: UsageStore | None = None,
    ):
        """Создаёт таблицы сервиса, учёта LLM, загрузок и очереди (запись в базу) и
        запускает поток ядра. `App` здесь не создаётся. `app_factory`, `store` и `usage`
        подменяются в тестах. Недоступная база — `DatabaseError`."""
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
        """Закрывает `App` (в потоке ядра) и останавливает поток ядра; при остановке API."""
        if self._app is not None:
            app, self._app = self._app, None
            await self._core.run(app.aclose())
        self._core.close()

    # ---- App в потоке ядра ----
    async def _ensure_app(self) -> App:
        """App, созданный лениво в потоке ядра; вызывается только из цикла ядра.

        `prepare(resume=False)` проверяет сигнатуру индекса и ничего не дописывает в корпус
        (`EmbedderMismatchError`, если нужен `reindex`). При неудачной проверке App
        закрывается целиком (`aclose` закрывает и HTTP-клиенты моделей и Qdrant, а не только
        базы). Между проверкой `self._app is None` и присваиванием нет `await` на успешном
        пути, поэтому параллельные операции в цикле ядра не создадут два App."""
        if self._app is None:
            app = self._factory(self.settings)
            try:
                app.pipeline.prepare(resume=False)
            except BaseException:
                await app.aclose()
                raise
            self._app = app
        return self._app

    async def _drop_app(self) -> None:
        """Закрывает и забывает App после обрыва соединения; ошибка закрытия только
        логируется, чтобы не заслонить исходную."""
        if self._app is not None:
            app, self._app = self._app, None
            try:
                await app.aclose()
            except Exception as exc:
                self._log.warning("app_close_failed", error_type=type(exc).__name__)

    async def _with_app[T](self, operation: Callable[[App], Awaitable[T]], failure: str) -> T:
        """Выполняет `operation(app)` в потоке ядра и переводит ошибки в `ServiceError`.

        `failure` — текст для 503. `ServiceError` из операции проходит как есть;
        `EmbedderMismatchError` → 409; обрыв соединения (`_CONNECTION_ERRORS`) → 503 и App
        закрывается, чтобы следующий запрос создал его с новыми соединениями; прочие
        исключения → 503. Исходное исключение не прикрепляется (`from None`), в лог — только
        его тип."""

        async def run() -> T:
            try:
                return await operation(await self._ensure_app())
            except ServiceError:
                raise
            except EmbedderMismatchError:
                raise ServiceError(409, "Индекс требует `mnogobase reindex`") from None
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
        """Очередь на время блока: своё соединение на каждый вызов, закрывается в конце."""
        queue = JobQueue(database_target(self.settings))
        try:
            yield queue
        finally:
            queue.close()

    def _queue_has_room(self, queue: JobQueue) -> None:
        """429, если `pending` + `running` задач уже `service.max_pending_jobs` или больше.
        Проверка без блокировки: параллельные запросы могут немного превысить предел."""
        counts = queue.counts()
        if (
            sum(counts.get(s, 0) for s in ("pending", "running"))
            >= self.settings.service.max_pending_jobs
        ):
            raise ServiceError(429, "Очередь обработки заполнена")

    def check_budget(self, now: float | None = None) -> None:
        """429, если за текущие сутки (UTC) израсходован `service.daily_token_limit`:
        защита от случайного цикла или украденной сессии. Учитываются все вызовы LLM,
        включая ingest worker'а. `None` в настройке отключает проверку; `now` — для тестов."""
        limit = self.settings.service.daily_token_limit
        if limit is None:
            return
        if self.usage.tokens_since(_day_start(time.time() if now is None else now)) >= limit:
            raise ServiceError(429, "Достигнут дневной лимит токенов LLM")

    # ---- документы и задачи (только очередь; в корпус пишет worker) ----
    def upload_suffix(self, filename: str) -> str:
        """Проверяет имя и тип загружаемого файла до чтения тела запроса и возвращает
        расширение в нижнем регистре (с точкой).

        422 — пустое или служебное имя, разделители путей или NUL, длина больше 255,
        расширение не из `parsing.extensions`."""
        if (
            not filename
            or filename in (".", "..")
            or any(c in filename for c in "/\\\x00")
            or len(filename) > 255
        ):
            raise ServiceError(422, "Недопустимое имя файла")
        suffix = Path(filename).suffix.lower()
        if suffix.lstrip(".") not in {
            x.lower().lstrip(".") for x in self.settings.parsing.extensions
        }:
            raise ServiceError(422, "Неподдерживаемый тип документа")
        return suffix

    def staging_dir(self) -> Path:
        """Каталог для временных файлов загрузки (создаётся при необходимости): тот же, что
        у итоговых, чтобы перенос был атомарным `rename` в пределах одной файловой системы."""
        self.uploads.root.mkdir(parents=True, exist_ok=True)
        return self.uploads.root

    def upload_file(self, filename: str, staged: Path, sha256: str) -> dict:
        """Ставит в очередь уже записанный во временный файл `staged` документ и возвращает
        задачу `ingest` (как в `/api/jobs`).

        `sha256` — хеш содержимого, посчитанный при приёме; `doc_id = sha256[:16]`, как в
        `ids.file_doc_id`. Порядок: проверки (имя, суточный лимит, пустой файл, размер,
        сигнатура формата), запись `uploads`, поиск уже идущей или выполненной задачи того же
        содержимого (тогда она и возвращается, файл не трогается), проверка места в очереди и
        `enqueue(force=True)`. `force` нужен, чтобы вернуть документ, удалённый раньше
        (`desired` = `remove:<doc_id>`). Файл переносится на место загрузки под блокировкой
        пути внутри `enqueue`; если он не понадобился (дубликат или ошибка), его удаляет
        вызывающий.

        422 — неверное имя/тип, пустой файл или содержимое не того формата; 413 — больше
        `service.max_upload_mb`; 429 — суточный лимит или переполненная очередь; 409 — запись
        `uploads` указывает за пределы каталога загрузок."""
        suffix = self.upload_suffix(filename)
        self.check_budget()
        size = staged.stat().st_size
        if not size:
            raise ServiceError(422, "Документ пуст")
        if size > self.settings.service.max_upload_mb * 1024 * 1024:
            raise ServiceError(413, "Документ слишком большой")
        _check_signature(staged, suffix)
        doc_id = sha256[:16]
        previous = self.uploads.get(doc_id)
        path = Path(previous["path"]) if previous else self.uploads.root / (doc_id + suffix)
        try:
            self.uploads.contained(path, must_exist=False)
        except FileNotFoundError:
            raise ServiceError(409, "Хранилище загрузок в несогласованном состоянии") from None
        # Метаданные до постановки в очередь: если процесс прервётся после enqueue, имя уже
        # записано; повторная загрузка допишет его, если прервалось раньше.
        self.uploads.add(doc_id, filename, path)
        with self._queue() as queue:
            existing = queue.find(path, doc_id)
            if (
                existing is not None
                and existing.status not in ("failed", "superseded")
                and queue.is_current(existing)
                and path.is_file()
            ):
                return self._job(existing)
            self._queue_has_room(queue)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Файл кладётся под блокировкой пути: идущая remove-задача не удалит его следом.
            job_id = queue.enqueue(path, doc_id, force=True, on_locked=lambda: staged.replace(path))
            return self._job(queue.get(job_id))

    def _upload_path(self, doc_id: str) -> Path:
        """Путь существующего файла загрузки `doc_id` внутри каталога загрузок.

        404 при любой проблеме (неверный формат id, нет записи, нет файла, путь вне каталога
        или ссылка) — одинаковый ответ не раскрывает, что именно не так."""
        if not _DOC_ID.fullmatch(doc_id):
            raise ServiceError(404, "Источник недоступен")
        row = self.uploads.get(doc_id)
        if row is None:
            raise ServiceError(404, "Источник недоступен")
        try:
            return self.uploads.contained(Path(row["path"]))
        except FileNotFoundError:
            raise ServiceError(404, "Источник недоступен") from None

    def read_source(self, doc_id: str) -> tuple[bytes, str]:
        """Содержимое загруженного файла и его исходное имя.

        Файл читается в память целиком (не больше `service.max_upload_mb`), поэтому
        одновременное удаление не обрывает уже начатый ответ. 404 — нет загрузки с таким
        id; 413 — файл больше предела (например, предел уменьшили после загрузки)."""
        path = self._upload_path(doc_id)
        limit = self.settings.service.max_upload_mb * 1024 * 1024
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ServiceError(413, "Документ слишком большой")
        return data, self.uploads.get(doc_id)["name"]

    async def documents(self, limit: int = 50, offset: int = 0) -> list[dict]:
        """Страница документов корпуса из registry: загруженные через API и добавленные из
        CLI или папки.

        У загруженных — исходное имя и `url` для скачивания; у прочих — только имя файла (полный
        путь на сервере не раскрывается) и `url = None`. 422 — неверная пагинация, 503 —
        ядро недоступно."""
        check_pagination(limit, offset)

        async def run(app: App) -> list[dict]:
            rows = app.registry.files()
            known = {row.path for row in rows}
            # загрузки, которые worker ещё не зарегистрировал: видны сразу после загрузки
            queued = [
                {
                    "id": u["doc_id"],
                    "name": u["name"],
                    "status": "queued",
                    "uploaded": True,
                    "url": _source_url(u["doc_id"]),
                }
                for u in self.uploads.all()
                if u["path"] not in known
            ]
            out = []
            for row in rows:
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
            return (queued + out)[offset : offset + limit]

        return await self._with_app(run, "Список документов недоступен")

    def _upload_names(self) -> dict[str, str]:
        """`doc_id` → имя, под которым файл загрузили (для подписей задач)."""
        return {u["doc_id"]: u["name"] for u in self.uploads.all()}

    def _job(self, job) -> dict:
        """`_job` для одной задачи: имя загрузки берётся одним запросом."""
        upload = self.uploads.get(job.fingerprint.removeprefix("remove:"))
        return _job(job, {upload["doc_id"]: upload["name"]} if upload else None)

    def _name(self, doc_id: str, path: str | None) -> str:
        """Имя документа для показа: исходное имя загрузки, иначе имя файла без каталога."""
        upload = self.uploads.get(doc_id)
        if upload is not None:
            return upload["name"]
        return Path(path).name if path else doc_id

    async def document(self, doc_id: str) -> dict:
        """Карточка документа: имя, статус, стадии с ошибками, число чанков и сущности.

        Загрузка, которую worker ещё не взял, — `status="queued"` без стадий и сущностей.
        У сущности `has_page` — есть ли актуальная страница wiki, `slug` — её адрес.
        404 — неверный id или документ неизвестен; 503 — ядро недоступно."""
        if not _DOC_ID.fullmatch(doc_id):
            raise ServiceError(404, "Документ не найден")

        async def run(app: App) -> dict:
            upload = self.uploads.get(doc_id)
            rows = {r.path: r for r in app.registry.files() if r.doc_id == doc_id}
            base = {
                "id": doc_id,
                "uploaded": upload is not None,
                "url": _source_url(doc_id) if upload is not None else None,
            }
            if not rows:
                if upload is None:
                    raise ServiceError(404, "Документ не найден")
                return {
                    **base,
                    "name": upload["name"],
                    "status": "queued",
                    "stages": [],
                    "chunks": 0,
                    "entities": [],
                }
            path = upload["path"] if upload is not None and upload["path"] in rows else min(rows)
            pages = {p.page_id: p.slug for p in app.graph.wiki_pages()}
            entities = sorted(
                app.graph.get_entities(app.registry.doc_entities(doc_id)),
                key=lambda e: e.name.lower(),
            )
            return {
                **base,
                "name": self._name(doc_id, path),
                "status": rows[path].status,
                "stages": app.registry.stages(doc_id),
                "chunks": app.graph.count_doc_chunks(doc_id),
                "entities": [
                    {
                        "entity_id": e.entity_id,
                        "name": e.name,
                        "type": e.type,
                        "has_page": e.entity_id in pages,
                        "slug": pages.get(e.entity_id),
                    }
                    for e in entities
                ],
            }

        return await self._with_app(run, "Документ недоступен")

    async def document_chunks(self, doc_id: str, limit: int = 50, offset: int = 0) -> dict:
        """Страница чанков документа по порядку (для просмотра в браузере) и их общее число.
        404 — документа нет в графе; 422 — неверная пагинация; 503 — ядро недоступно."""
        check_pagination(limit, offset)
        if not _DOC_ID.fullmatch(doc_id):
            raise ServiceError(404, "Документ не найден")

        async def run(app: App) -> dict:
            found = app.graph.doc_chunks(doc_id)
            if found is None:
                raise ServiceError(404, "Документ не найден")
            doc, chunks = found
            return {
                "document": {
                    "id": doc_id,
                    "name": self._name(doc_id, doc.path),
                    "title": doc.title,
                    "n_pages": doc.n_pages,
                },
                "total": len(chunks),
                "chunks": [
                    {
                        "chunk_id": c.chunk_id,
                        "idx": c.idx,
                        "text": c.text,
                        "headings": c.headings,
                        "page_start": c.page_start,
                        "page_end": c.page_end,
                    }
                    for c in chunks[offset : offset + limit]
                ],
            }

        return await self._with_app(run, "Документ недоступен")

    async def chunk(self, chunk_id: str) -> dict:
        """Полный текст одного фрагмента (по клику на цитату) с именем документа и ссылкой
        на исходник, если он загружен через API. 404 — неверный id или фрагмента уже нет."""
        if not _CHUNK_ID.fullmatch(chunk_id):
            raise ServiceError(404, "Фрагмент не найден")

        async def run(app: App) -> dict:
            found = app.graph.chunks_by_ids([chunk_id])
            if not found:
                raise ServiceError(404, "Фрагмент не найден")
            view = found[0]
            uploaded = self.uploads.get(view.doc_id) is not None
            return {
                "chunk_id": view.chunk_id,
                "doc_id": view.doc_id,
                "name": self._name(view.doc_id, view.path),
                "text": view.text,
                "headings": view.headings,
                "page": view.page,
                "url": _source_url(view.doc_id) if uploaded else None,
            }

        return await self._with_app(run, "Фрагмент недоступен")

    def jobs(self, limit: int = 50, offset: int = 0) -> list[dict]:
        """Страница задач очереди, новые первыми; 422 — неверная пагинация."""
        check_pagination(limit, offset)
        with self._queue() as queue:
            names = self._upload_names()
            return [_job(j, names) for j in queue.jobs(limit, offset)]

    def retry_job(self, job_id: int) -> dict:
        """Перезапускает задачу, если она `failed` (иначе ничего не меняет), и возвращает её
        текущее состояние; 404 — нет задачи."""
        with self._queue() as queue:
            if queue.get(job_id) is None:
                raise ServiceError(404, "Задача не найдена")
            queue.retry(job_id)
            return self._job(queue.get(job_id))

    async def remove_document(self, doc_id: str) -> dict:
        """Ставит удаление документа из корпуса и возвращает задачу `remove`.

        Пути документа берутся из registry (у одного содержимого может быть несколько копий)
        плюс файл загрузки, если его ещё нет в registry (документ загружен, но ещё не
        обработан). На каждый путь ставится задача `remove` с версией `remove:<doc_id>` и
        `force=True`; возвращается последняя. 404 — неверный id или документ неизвестен,
        429 — очередь переполнена, 503 — ядро недоступно."""
        if not _DOC_ID.fullmatch(doc_id):
            raise ServiceError(404, "Документ не найден")

        async def paths(app: App) -> list[str]:
            return app.registry.paths_for_doc(doc_id)

        known = await self._with_app(paths, "Удаление документов недоступно")
        upload = self.uploads.get(doc_id)
        if upload is not None and upload["path"] not in known:
            known.append(upload["path"])
        if not known:
            raise ServiceError(404, "Документ не найден")
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
            return self._job(queue.get(job_ids[-1]))

    # ---- чтение корпуса ----
    @staticmethod
    def _item(source: dict) -> dict:
        """Источник ответа для API: вместо полного пути — только имя файла; у фрагментов
        документа — `doc_id` и `url` исходника (из `ref` = `chunk_id`), у страниц wiki —
        `url` страницы."""
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
        """Источники для ответа API. Загруженный файл лежит на диске как
        `<doc_id><расширение>`: вместо этого имени показывается то, под которым его
        загрузили."""
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
        """Ретриверы всех режимов `{mode: Retriever}` поверх клиентов `App`."""
        return build_retrievers(
            app.vectors, app.graph, app.embedder, app.sparse, self.settings, app.reranker
        )

    async def search(self, question: str, mode: str = "rag", k: int = 8) -> list[dict]:
        """Поиск без генерации ответа: до `k` фрагментов режима `mode`.

        При `retrieval.translate` вопрос дополнительно переводится на английский (вызов
        LLM). Пустой корпус — пустой список. 422 — неверный вопрос, режим или `k`; 429 —
        суточный лимит; 409 — нужен `reindex`; 503 — ядро недоступно. Ждёт свободный слот
        семафора `service.max_concurrent_asks`."""
        question = _question(question)
        _mode(mode)
        if not 1 <= k <= 100:
            raise ServiceError(422, "Неверный лимит поиска")
        await asyncio.to_thread(self.check_budget)

        async def run(app: App) -> list[dict]:
            if not app.registry.doc_ids():
                return []
            translator = QueryTranslator(app.llm) if self.settings.retrieval.translate else None
            alt = await translator.alt_queries(question) if translator else []
            items = self._retrievers(app)[mode].retrieve(question, k, alt)
            return self._items([x.model_dump() for x in items])

        async with self._asks:
            return await self._with_app(run, "Поиск недоступен")

    async def _answer(
        self, app: App, question: str, mode: str, history: list[dict] | None = None
    ) -> dict:
        """Ответ с источниками; выполняется в потоке ядра.

        С `history` (последние сообщения диалога) вопрос сначала переписывается LLM
        (`chat_query.md`, задача `query`) в самостоятельный запрос: «а что про него?»
        становится полным вопросом. История — недоверенные данные: каждое сообщение
        обрезается до 4000 символов, а из ответа LLM берётся только первая строка, и лишь
        если он не длиннее 4000 символов. В ответе `question` — исходный вопрос, `query` —
        тот, по которому искали. Пустой корпус — `NO_SOURCES` без вызова LLM."""
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
        """Ответ на отдельный вопрос без истории. Коды ошибок как у `search`."""
        question = _question(question)
        _mode(mode)
        await asyncio.to_thread(self.check_budget)
        async with self._asks:
            return await self._with_app(
                lambda app: self._answer(app, question, mode),
                "Модель или база знаний недоступны",
            )

    async def send_message(
        self, user_id: str, conversation_id: str, question: str, mode: str = "rag"
    ) -> dict:
        """Вопрос в диалоге: сохраняет вопрос, отвечает с учётом истории, сохраняет ответ и
        возвращает сообщение ассистента.

        История — сообщения со статусом `done` из `service.history_messages` последних
        (0 — без истории). Если ответить не удалось, в диалог пишется сообщение со статусом
        `error` и нейтральным текстом, а `ServiceError` пробрасывается. 404 — нет диалога
        или он чужой; остальные коды как у `ask`. Синхронные обращения к базе сервиса идут
        через `asyncio.to_thread`, чтобы не блокировать цикл веб-сервера."""
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
        await asyncio.to_thread(store.set_title_if_empty, user_id, conversation_id, question[:60])
        await asyncio.to_thread(
            store.append_message, user_id, conversation_id, "user", question, mode=mode
        )
        try:
            async with self._asks:
                answer = await self._with_app(
                    lambda app: self._answer(app, question, mode, history),
                    "Модель или база знаний недоступны",
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
            meta=_meta(answer),
        )

    def history(
        self, user_id: str, conversation_id: str, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        """Страница сообщений диалога; у каждого источника добавляется `available` — есть ли
        ещё загруженный файл или страница wiki (документ могли удалить после ответа).
        404 — нет диалога, 422 — неверная пагинация."""
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
        """Страница списка актуальных страниц wiki (устаревшие `stale` скрыты), без путей на
        диске. 422 — неверная пагинация, 503 — ядро недоступно."""
        check_pagination(limit, offset)

        async def run(app: App) -> list[dict]:
            return [
                {k: v for k, v in row.model_dump().items() if k != "path"}
                for row in app.graph.wiki_pages()[offset : offset + limit]
            ]

        return await self._with_app(run, "Wiki недоступна")

    async def wiki_page(self, page_id: str) -> dict:
        """Markdown страницы wiki с заголовком и slug. 404 — страницы нет, она устарела или
        её файл отсутствует либо лежит вне `wiki.dir`; 503 — ядро недоступно."""

        async def run(app: App) -> dict:
            page = app.graph.wiki_page(page_id)
            if page is None or page.stale:
                raise ServiceError(404, "Страница wiki не найдена")
            root = self.settings.wiki.dir.resolve()
            path = (self.settings.wiki.dir / page.path).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ServiceError(404, "Страница wiki недоступна")
            return {
                "id": page.page_id,
                "title": page.title,
                "slug": page.slug,
                "content": path.read_text(encoding="utf-8"),
            }

        return await self._with_app(run, "Wiki недоступна")

    async def graph_search(self, query: str, limit: int = 20) -> list[dict]:
        """Сущности графа по полнотекстовому поиску в Neo4j, лучшие первыми.
        422 — неверный `limit`, 503 — ядро недоступно."""
        check_pagination(limit, 0)

        async def run(app: App) -> list[dict]:
            found = (
                app.graph.get_entity(eid) for eid, _ in app.graph.fulltext_entities(query, limit)
            )
            # сущность могла исчезнуть между двумя запросами (параллельное удаление документа)
            return [entity.model_dump() for entity in found if entity is not None]

        return await self._with_app(run, "Граф недоступен")

    async def graph_neighbors(self, entity_id: str, depth: int = 1, limit: int = 50) -> dict:
        """Окрестность сущности глубиной `depth` (1–3): до `limit` связей и все их концы.
        404 — нет сущности, 422 — неверные `depth` / `limit`, 503 — ядро недоступно."""
        check_pagination(limit, 0)
        if not 1 <= depth <= 3:
            raise ServiceError(422, "Глубина графа должна быть от 1 до 3")

        async def run(app: App) -> dict:
            entity = app.graph.get_entity(entity_id)
            if entity is None:
                raise ServiceError(404, "Сущность не найдена")
            relations = app.graph.neighborhood([entity_id], depth, limit)
            ids = {entity_id} | {r.src_id for r in relations} | {r.dst_id for r in relations}
            found = (app.graph.get_entity(eid) for eid in sorted(ids))
            nodes = [node.model_dump() for node in found if node is not None]
            return {"nodes": nodes, "relations": [r.model_dump() for r in relations]}

        return await self._with_app(run, "Граф недоступен")

    # ---- состояние ----
    def status(self, now: float | None = None) -> dict:
        """Состояние сервиса для `/api/status`: число задач по статусам, выполняемые задачи,
        жив ли worker (отметка свежее `HEARTBEAT_STALE_S`) и расход LLM за сутки и календарный
        месяц (UTC). Работает без `App`, поэтому отвечает, даже когда модели недоступны."""
        now = time.time() if now is None else now
        with self._queue() as queue:
            counts = queue.counts()
            heartbeat = queue.last_heartbeat()
            names = self._upload_names()
            running = [_job(j, names) for j in queue.running()]
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
    """Начало суток (UTC) для unix-времени `now`."""
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
    """422, если первые байты файла не соответствуют двоичному формату его расширения.

    WebP проверяется отдельно (`RIFF....WEBP`, сигнатура не в начале подряд); расширения без
    записи в `_SIGNATURES` проходят без проверки."""
    with path.open("rb") as stream:
        head = stream.read(16)
    if suffix == ".webp":
        valid = head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    else:
        valid = head.startswith(_SIGNATURES.get(suffix, (b"",)))
    if not valid:
        raise ServiceError(422, "Содержимое файла не соответствует его типу")
