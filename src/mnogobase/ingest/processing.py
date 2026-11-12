"""Ingest с замерами и «отпечаток» проиндексированного корпуса.

`measured_ingest` — обёртка над `Pipeline.ingest`, через которую идут `mnogobase ingest`,
worker очереди (`worker run`) и `eval ingest`. Она меряет время и токены LLM по стадиям
(`Measurement`) и дописывает строку в JSONL (по умолчанию `<data_dir>/processing.jsonl`).
Эта предобработка общая для всех режимов поиска (rag / wiki / graph), поэтому её стоимость
в оценке считается один раз.

`corpus_snapshot` описывает состояние корпуса отпечатком (sha256) строк registry и файлов
wiki. Оценка (`eval`) закрепляет его в манифесте, чтобы понять, что ответы получены на том
же корпусе. Снимок — это не транзакция над внешними базами (Qdrant и Neo4j в него не
входят): чтобы он соответствовал ответам, писатель должен держать advisory-lock `ingest`
и пока снимает его, и пока генерирует ответы.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from mnogobase.core.config import Settings
from mnogobase.core.database import connect, database_target, target_exists
from mnogobase.core.files import append_jsonl, fingerprint
from mnogobase.core.ids import file_doc_id
from mnogobase.core.progress import NULL_PROGRESS, NullProgress

# что входит в токены строки замера (пишется в каждую строку как пояснение для отчёта)
TOKEN_SCOPE = (
    "Main LLM successful calls only; excludes vision, embeddings, reranker and failed calls."
)


def corpus_snapshot(settings: Settings) -> dict:
    """Снимок корпуса: отпечатки проекта и содержимого и простые счётчики.

    Возвращает словарь: `project` — отпечаток того, где лежат данные (схема PostgreSQL,
    настройки Qdrant, адрес и пользователь Neo4j); `fingerprint` — отпечаток строк `files`,
    `meta`, `stages`, `pending_removals`, `dirty_entities` и sha256 каждого `.md` в wiki
    (без `.obsidian`); `documents` — число различных `doc_id`; `wiki_pages` — число файлов
    wiki. Если registry ещё не создана, таблицы считаются пустыми (ничего не создаётся).

    Только чтение: одно короткое соединение с PostgreSQL и чтение всех файлов wiki."""
    db = database_target(settings)
    files, meta, stages, removals, dirty = [], [], [], [], []
    if target_exists(db):
        connection = connect(db)
        try:
            files = connection.execute(
                "SELECT path,doc_id,status FROM files ORDER BY path"
            ).fetchall()
            meta = connection.execute("SELECT key,value FROM meta ORDER BY key").fetchall()
            stages = connection.execute(
                "SELECT doc_id,stage,status,attempts FROM stages ORDER BY doc_id,stage"
            ).fetchall()
            removals = connection.execute(
                "SELECT doc_id,plan_json FROM pending_removals ORDER BY doc_id"
            ).fetchall()
            dirty = connection.execute(
                "SELECT entity_id,marked_at FROM dirty_entities ORDER BY entity_id"
            ).fetchall()
        finally:
            connection.close()
    wiki = [
        (p.relative_to(settings.wiki.dir).as_posix(), hashlib.sha256(p.read_bytes()).hexdigest())
        for p in sorted(settings.wiki.dir.rglob("*.md"))
        if ".obsidian" not in p.parts
    ]
    project = fingerprint(
        {
            "state": str(db),
            "qdrant": settings.qdrant.model_dump(),
            "neo4j": {"uri": settings.neo4j.uri, "user": settings.neo4j.user},
        }
    )
    return {
        "project": project,
        "fingerprint": fingerprint(
            {
                "files": files,
                "meta": meta,
                "wiki": wiki,
                "stages": stages,
                "removals": removals,
                "dirty": dirty,
            }
        ),
        "documents": len({r[1] for r in files}),
        "wiki_pages": len(wiki),
    }


class Measurement(NullProgress):
    """`ProgressSink`, который меряет стадии и передаёт все события дальше в `delegate`.

    Стадия — непрерывный отрезок событий `step` с одним именем; она закрывается следующим
    `step` с другим именем, `file_started`, `file_done` или `finish`. Для каждой
    записываются длительность (мс), число выполненных и упавших шагов, `total` и разность
    токенов LLM (`llm.usage.tokens_in` / `tokens_out`) за отрезок. Если у LLM нет `usage`,
    токены — None. Одна и та же стадия разных файлов даёт отдельные записи.

    `clock` подменяется в тестах."""

    def __init__(self, delegate, llm=None, *, clock=time.perf_counter):
        self.delegate, self.llm, self.clock = delegate, llm, clock
        self.started = clock()
        self.initial = self._usage()
        # открытая стадия: (запись, момент начала, токены на момент начала) или None
        self.active = None
        self.stages = []

    def _usage(self):
        """Текущие счётчики токенов LLM (вход, выход) или (None, None)."""
        usage = getattr(self.llm, "usage", None)
        return (usage.tokens_in, usage.tokens_out) if usage is not None else (None, None)

    @staticmethod
    def _delta(before, after):
        """Поэлементная разность счётчиков; None, если хотя бы одно значение неизвестно."""
        return [
            b - a if a is not None and b is not None else None
            for a, b in zip(before, after, strict=True)
        ]

    def _close(self):
        """Закрывает открытую стадию: дописывает длительность и токены и кладёт в список."""
        if self.active is not None:
            stage, started, usage = self.active
            stage["duration_ms"] = max(0, round((self.clock() - started) * 1000))
            stage["tokens_in"], stage["tokens_out"] = self._delta(usage, self._usage())
            self.stages.append(stage)
            self.active = None

    # события файлов закрывают открытую стадию, чтобы стадии разных файлов не сливались
    def files_found(self, total):
        self.delegate.files_found(total)

    def file_started(self, path):
        self._close()
        self.delegate.file_started(path)

    def file_done(self, status):
        self._close()
        self.delegate.file_done(status)

    def step(self, name, total=None):
        """Открывает стадию `name` или, если она уже открыта, только обновляет `total`
        (например, `extract` сообщает число чанков вторым вызовом)."""
        if self.active is None or self.active[0]["stage"] != name:
            self._close()
            self.active = (
                {"stage": name, "completed": 0, "failed": 0, "total": total},
                self.clock(),
                self._usage(),
            )
        else:
            self.active[0]["total"] = total
        self.delegate.step(name, total)

    def advance(self, n=1, *, failed=False):
        if self.active:
            self.active[0]["completed"] += n
            self.active[0]["failed"] += n if failed else 0
        self.delegate.advance(n, failed=failed)

    def finish(self):
        """Закрывает последнюю стадию и возвращает итог: общая длительность, токены за весь
        замер и список стадий (поля для строки `processing.jsonl`)."""
        self._close()
        tokens_in, tokens_out = self._delta(self.initial, self._usage())
        return {
            "duration_ms": max(0, round((self.clock() - self.started) * 1000)),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "stages": self.stages,
        }


async def measured_ingest(
    application,
    paths,
    *,
    phase=None,
    output: Path | None = None,
    settings: Settings | None = None,
    progress=NULL_PROGRESS,
    verify_sources=False,
    expected=None,
    snapshot=True,
    **options,
):
    """Запускает `application.pipeline.ingest(paths, **options)` с замером и пишет строку
    замера в `output` (JSONL), даже если ingest упал.

    Аргументы:
    - `phase` — "build" (первая загрузка) или "update"; по умолчанию "build", если снимок
      до ingest показал пустой корпус, иначе "update". Другое значение — `ValueError`.
    - `output` — файл замеров, по умолчанию `<data_dir>/processing.jsonl`.
    - `verify_sources` (worker) — до ingest берётся подпись каждого источника (размер,
      mtime, inode, `doc_id`); если `expected[path]` задан и не совпадает с `doc_id`, задача
      устарела. После успешного ingest каждый источник должен оказаться в
      processed/skipped и не измениться за время обработки. Любое нарушение —
      `SourceChangedError` (worker повторит задачу).
    - `snapshot=False` (worker) не снимает полный снимок корпуса до и после: на очереди из
      N задач это давало бы O(N²) работы.
    - `options` передаются в `Pipeline.ingest` (`retry_failed`, `run_id`, `build_wiki`).

    Статус строки: "ok"; "error" — есть упавшие файлы или страницы wiki, или вылетело
    `Exception` (тогда в `error` имя его класса); "interrupted" — ingest прервало
    исключение вне `Exception` (`KeyboardInterrupt`, отмена задачи `asyncio`), строка при
    этом всё равно пишется. Возвращает `IngestReport`; исключения пробрасываются."""
    if phase is not None and phase not in ("build", "update"):
        raise ValueError("phase должен быть build или update")
    settings = settings or application.settings
    output = output or settings.data_dir / "processing.jsonl"
    before = corpus_snapshot(settings) if snapshot else None
    phase = phase or ("build" if before is not None and before["documents"] == 0 else "update")
    measurement = Measurement(progress, getattr(application, "llm", None))
    row = {
        "id": uuid.uuid4().hex,
        "ts": datetime.now(UTC).isoformat(),
        "phase": phase,
        "scope": "shared preprocessing for implemented retrieval modes",
        "token_scope": TOKEN_SCOPE,
        "before": before,
        "status": "interrupted",
    }
    report = None
    try:
        signatures = {}
        if verify_sources:
            for source in paths:
                source = Path(source).resolve()
                signatures[str(source)] = _source_signature(source)
                if expected and expected.get(str(source)) != signatures[str(source)][-1]:
                    raise SourceChangedError(
                        "источник изменился после того, как задание взяли в работу"
                    )
        report = await application.pipeline.ingest(paths, progress=measurement, **options)
        row["status"] = "error" if report.failed or (report.wiki and report.wiki.failed) else "ok"
        if verify_sources and row["status"] == "ok":
            acknowledged = {str(Path(p).resolve()) for p in [*report.processed, *report.skipped]}
            for path, signature in signatures.items():
                if path not in acknowledged:
                    raise SourceChangedError("конвейер не обработал и не пропустил источник")
                if _source_signature(Path(path)) != signature:
                    raise SourceChangedError(
                        "источник изменился во время обработки; нужен повторный запуск"
                    )
        return report
    except Exception as exc:
        row.update(status="error", error=type(exc).__name__)
        raise
    finally:
        row.update(measurement.finish())
        if report is not None:
            row.update(
                processed=len(getattr(report, "processed", [])),
                skipped=len(getattr(report, "skipped", [])),
                failed=len(report.failed),
                wiki_failed=len(report.wiki.failed) if report.wiki else 0,
            )
        row["after"] = corpus_snapshot(settings) if snapshot else None
        append_jsonl(output, row)


class SourceChangedError(RuntimeError):
    """Источник задачи изменился между постановкой в очередь и концом обработки (или во
    время хэширования); результат ingest не соответствует задаче, её нужно повторить."""


def _source_signature(path: Path):
    """Подпись файла: (размер, mtime в нс, inode, `doc_id`).

    `stat` берётся до и после хэширования: если файл менялся, пока читался, хэш может не
    соответствовать ни одной версии, и бросается `SourceChangedError`."""
    before = path.stat()
    digest = file_doc_id(path)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ino,
    ):
        raise SourceChangedError("источник изменился, пока считался его хеш")
    return after.st_size, after.st_mtime_ns, after.st_ino, digest
