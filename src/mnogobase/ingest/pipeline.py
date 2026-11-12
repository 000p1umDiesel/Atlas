"""Конвейер ingest и удаления документов: единственный код, который пишет корпус.

Место в потоке данных. `Pipeline` собирает `mnogobase.app.build_app`; вызывают его CLI-команды
`ingest` / `remove` / `wiki build` и worker (через `ingest.processing.measured_ingest`), а
`reindex` использует `load_chunks` / `index_chunks`. Все пишущие вызовы идут под
advisory-lock `ingest` (`stores.registry.ingest_lock`), то есть писатель всегда один. Чтения
(`ask`, API) lock не берут и через `Pipeline` ничего не пишут: максимум `prepare(resume=False)`.

Стадии. Каждый документ проходит `registry.STAGES` строго по порядку:

- `parse` — `DoclingParser` разбирает файл (OCR, описания картинок от VLM) в свой кэш;
- `chunk` — `Chunker` режет документ на чанки; документ и чанки атомарно пишутся в кэш
  `<data_dir>/cache/<doc_id>.chunks.json` (`write_atomic`);
- `embed` — dense + sparse векторы чанков → Qdrant, затем узлы `Document` / `Chunk` → Neo4j;
- `extract` — LLM извлекает сущности и связи каждого чанка; результат ложится только в
  `extraction_cache` registry, хранилища не трогаются;
- `graph` — `EntityResolver` сливает сущности с уже известными (сам пишет их в Qdrant и
  Neo4j), затем по каждому чанку: id сущностей → payload чанка в Qdrant, MENTIONS + RELATED →
  Neo4j одной транзакцией; затронутые сущности ставятся в очередь `dirty_entities`.

После всех файлов `WikiBuilder.build` инкрементально перестраивает страницы dirty-сущностей.

Машина состояний стадий (таблица `stages`). Нет строки — `pending`. Перед запуском стадия
становится `running` (`attempts` +1), после всех её записей — `done`, при исключении —
`failed` с безопасным текстом ошибки (и файл в `files` тоже `failed`). Стадия `done`
никогда не перезапускается. `running` после сбоя процесса `prepare()` возвращает в
`pending`, и следующий запуск продолжает документ с прерванной стадии. `failed`
перезапускается только с `--retry-failed`, иначе файл сразу отчитывается как failed.
Документ без незавершённых стадий — `skipped`. Повтор стадии безопасен: детерминированные
id (`core.ids`) превращают каждую запись в идемпотентный upsert / `MERGE`.

Идентичность документа — его содержимое: `doc_id = sha256(байты файла)[:16]`. Одно
содержимое может лежать по нескольким путям (копии), строка `files` связывает путь с
`doc_id`. Изменённый файл получает новый `doc_id`; старая версия удаляется, если её не
держит другая живая копия (файл с тем же содержимым).

Журнал удаления. Удаление документа затрагивает Neo4j, Qdrant, файлы wiki и registry и не
может быть атомарным, поэтому оно журналируется в `pending_removals`:

1. `_removal_plan` (только чтение, пока данные ещё в графе) вычисляет `_RemovalPlan`:
   сущности, которые останутся без упоминаний, их страницы, страницы для перегенерации.
   В план входят и `doc_entities` — сущности, которые стадия `graph` уже записала через
   resolver, даже если MENTIONS до графа не дошли (стадия упала посреди чанка).
2. План пишется в журнал (`put_removal`) до любого изменения хранилищ.
3. `_apply_removal`: одна транзакция Neo4j (после её коммита граф — источник истины), затем
   Qdrant, очистка объединённых описаний, инвалидация страниц wiki, файлы wiki, registry;
   последний шаг — `drop_removal`.

Сбой на любом шаге оставляет запись в журнале. `finish_pending_removals` в начале следующего
`ingest` / `remove` / `wiki build` доводит её первой (`_resume_removal` заново сверяется с
текущим состоянием), и пока это не удалось, ничего другого не выполняется
(`PendingRemovalError`). `forgotten_paths` — пути, которые пользователь явно попросил забыть
(`mnogobase remove`): они не считаются живыми копиями, даже если файл ещё лежит на диске,
иначе явное удаление отменило бы само себя. Имена удалённых сущностей, ещё не попавшие в
отчёт wiki (`log.md`), копятся в meta-ключе `unreported_removed_entities`.

Порядок записи: вектор → Qdrant → Neo4j. Эмбеддинги считаются до любой записи, затем
пишется Qdrant, последним — граф, так что упавший embedder или Qdrant никогда не оставляет
граф впереди векторного индекса, а повтор стадии дописывает недостающее. Удаление идёт в
обратную сторону: сначала транзакция графа (точка невозврата), затем Qdrant.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field

from mnogobase.chunking.hybrid import Chunker
from mnogobase.core.config import Settings
from mnogobase.core.files import write_atomic
from mnogobase.core.ids import file_doc_id, normalize_name
from mnogobase.core.log import bind_context, get_logger, log_stage, safe_error, unbind_context
from mnogobase.core.models import ChunkRecord, DocumentRecord, EmbedInput
from mnogobase.core.progress import NULL_PROGRESS, ProgressSink
from mnogobase.embedding.base import (
    Embedder,
    SparseEncoder,
    embedder_signature,
    signature_mismatch,
)
from mnogobase.extraction.extractor import TYPES_CHANGED, Extractor, stale_entity_types
from mnogobase.extraction.resolver import EntityResolver, entity_embed_input
from mnogobase.ingest.removal import RemovalReport, select_removals
from mnogobase.parsing.docling_parser import DoclingParser
from mnogobase.stores.graph_store import GraphStore
from mnogobase.stores.qdrant_store import QdrantStore
from mnogobase.stores.registry import Registry
from mnogobase.wiki.builder import WikiBuilder, WikiReport

ENTITY_BATCH = 64  # сущностей на один запрос эмбеддера и одну запись в граф


class EmbedderMismatchError(RuntimeError):
    """Индекс нельзя использовать в текущем виде; сообщение говорит, какую команду запустить.

    Бросается из `Pipeline.prepare`, когда сигнатура embedder в registry `meta` (модель,
    размерность, шаблоны) не совпадает с текущим конфигом или там стоит маркер незавершённого
    `reindex`. Лечится `mnogobase reindex`."""


class InterruptedResetError(EmbedderMismatchError):
    """Прерванный `reset`: часть данных уже удалена, пока `mnogobase reset` не запущен снова.

    Наследует `EmbedderMismatchError`, чтобы вызывающий код обрабатывал оба случая одинаково
    («индекс непригоден, запусти команду»)."""


class ExtractionFailedError(RuntimeError):
    """Доля чанков без извлечения превысила `extract.max_failed_ratio`.

    Стадия (`extract` или `graph`) помечается failed; документ без знаний не должен
    становиться done."""


@dataclass
class IngestReport:
    """Итог `Pipeline.ingest`.

    `processed` / `skipped` — пути файлов, прошедших стадии в этом запуске или уже готовых;
    `failed` — путь → безопасный текст ошибки; `wiki` — отчёт сборки wiki (None при
    `build_wiki=False`)."""

    processed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    wiki: WikiReport | None = None  # ошибки wiki по страницам лежат в `wiki.failed`
    # задаётся, если в графе есть сущности, типизированные другим набором типов, чем в конфиге
    types_warning: str | None = None


class _PreviouslyFailed(Exception):
    """Внутренний сигнал `_ingest_file`: стадия документа упала в прошлом запуске, а
    `--retry-failed` не задан. Файл попадает в `IngestReport.failed` без повторного запуска
    стадии и без трейсбека в логе (ошибка уже записана тогда)."""


# ключ meta в registry: JSON-список имён удалённых сущностей, ещё не попавших в отчёт wiki
_UNREPORTED_REMOVALS = "unreported_removed_entities"


class PendingRemovalError(RuntimeError):
    """Удаление документа из журнала `pending_removals` не удаётся завершить.

    Пока журнал не пуст, `ingest` / `remove` / `wiki build` ничего другого не делают:
    сначала нужно устранить причину (обычно недоступное хранилище) или сделать `reset`."""


class _RemovalPlan(BaseModel):
    """Что подчищает удаление документа; хранится в журнале registry, пока не завершится.

    Вычисляется в режиме только чтения прямо перед транзакцией графа, поэтому совпадает с
    тем, что эта транзакция удаляет. Сериализуется в JSON (`pending_removals.plan_json`),
    поэтому новые поля должны иметь значения по умолчанию: старые записи журнала должны
    читаться."""

    removed_entities: dict[str, str]  # entity_id -> имя
    removed_pages: list[tuple[str, str]]  # (page_id, путь относительно wiki)
    dirty: list[str]  # оставшиеся сущности, чьи страницы нужно перегенерировать
    relinked: int  # сколько из них лишь ссылались на удалённую страницу
    # пути, которые явно попросили забыть (`remove`); никогда не считаются живыми копиями
    forgotten_paths: list[str] = Field(default_factory=list)
    # сущности, которые остаются, но упоминались в документе: их объединённые описания
    # могли опираться на удаляемый источник и стираются
    affected_entities: list[str] = Field(default_factory=list)
    # сущности, записанные стадией graph документа, в том числе без MENTIONS (стадия упала)
    touched_entities: list[str] = Field(default_factory=list)


def _holds(path: str, doc_id: str) -> bool:
    """Существует ли файл по `path` и с этим ли он сейчас содержимым (строки registry и
    записанные пути документов могут устареть: файл переименовали, удалили или изменили)."""
    file = Path(path)
    return file.is_file() and file_doc_id(file) == doc_id


class Pipeline:
    """Ingest, удаление и перенаправление документов поверх registry, Qdrant, Neo4j и wiki.

    Все зависимости передаются готовыми (их создаёт и закрывает `build_app`); сам `Pipeline`
    соединений не открывает и не закрывает. Методы, которые пишут, вызываются только под
    advisory-lock `ingest`. Методы не потокобезопасны и рассчитаны на одного писателя."""

    def __init__(
        self,
        settings: Settings,
        registry: Registry,
        parser: DoclingParser,
        chunker: Chunker,
        embedder: Embedder,
        sparse: SparseEncoder,
        vectors: QdrantStore,
        graph: GraphStore,
        extractor: Extractor,
        resolver: EntityResolver,
        wiki: WikiBuilder,
    ):
        self._s = settings
        self._registry = registry
        self._parser = parser
        self._chunker = chunker
        self._embedder = embedder
        self._sparse = sparse
        self._vectors = vectors
        self._graph = graph
        self._extractor = extractor
        self._resolver = resolver
        self._wiki = wiki
        self._log = get_logger(__name__)

    # ---- подготовка ----
    def prepare(self, check_embedder: bool = True, resume: bool = True) -> None:
        """Проверяет, что индексом можно пользоваться, и готовит хранилища.

        Шаги: (1) флаг незавершённого `reset` в meta → `InterruptedResetError`;
        (2) при `check_embedder` сигнатура embedder в meta должна совпадать с текущей, иначе
        `EmbedderMismatchError` (маркер прерванного `reindex` тоже не совпадает);
        (3) коллекции Qdrant и схема Neo4j создаются, если их нет (сетевые вызовы);
        (4) у нового индекса сигнатура записывается compare-and-set;
        (5) при `resume` стадии `running` от упавшего прошлого запуска возвращаются в
        `pending`. Читающие вызовы (ask, API) передают `resume=False`: они работают без lock
        и не должны трогать стадии, которые сейчас выполняет писатель."""
        if self._registry.get_meta("reset") is not None:
            raise InterruptedResetError(
                "прерванный `mnogobase reset` оставил часть данных; запустите `mnogobase reset` ещё раз"
            )
        signature = embedder_signature(self._embedder)
        stored = self._registry.get_meta("embedder")
        mismatch = signature_mismatch(stored, self._embedder)
        if check_embedder and mismatch:
            raise EmbedderMismatchError(f"{mismatch}; запустите `mnogobase reindex`")
        self._vectors.ensure_collections()
        self._graph.ensure_schema()
        if stored is None:
            # новый индекс: compare-and-set, потому что ask / compare работают без lock и не
            # должны затирать то, что тем временем записал параллельный reindex
            self._registry.set_meta_if_absent("embedder", signature)
        if resume:
            resumed = self._registry.reset_running()
            if resumed:
                self._log.warning("resuming_interrupted_stages", count=resumed)

    def discover(self, paths: Sequence[Path]) -> list[Path]:
        """Раскрывает файлы и каталоги в список файлов для ingest.

        Каталоги обходятся рекурсивно в отсортированном порядке; берутся только расширения из
        `parsing.extensions`, скрытые файлы и каталоги (имя с точки) внутри переданного
        каталога пропускаются. Возвращает абсолютные пути без повторов, в порядке обхода."""
        exts = {f".{e.lower().lstrip('.')}" for e in self._s.parsing.extensions}
        found: list[Path] = []
        for path in paths:
            path = Path(path)
            if path.is_dir():
                for file in sorted(path.rglob("*")):
                    hidden = any(part.startswith(".") for part in file.relative_to(path).parts)
                    if file.is_file() and file.suffix.lower() in exts and not hidden:
                        found.append(file)
            elif path.is_file() and path.suffix.lower() in exts:
                found.append(path)
        return list(dict.fromkeys(f.resolve() for f in found))

    # ---- кэш чанков ----
    def chunks_path(self, doc_id: str) -> Path:
        """Путь кэша чанков документа: `<data_dir>/cache/<doc_id>.chunks.json`."""
        return self._s.data_dir / "cache" / f"{doc_id}.chunks.json"

    def _save_chunks(self, doc: DocumentRecord, chunks: list[ChunkRecord]) -> None:
        """Атомарно записывает документ и его чанки в кэш (сбой не оставит полфайла)."""
        path = self.chunks_path(doc.doc_id)
        payload = {"document": doc.model_dump(), "chunks": [c.model_dump() for c in chunks]}
        write_atomic(path, json.dumps(payload, ensure_ascii=False))

    def load_chunks(self, doc_id: str) -> tuple[DocumentRecord, list[ChunkRecord]]:
        """Документ и чанки из кэша чанков.

        Битый или пропавший кэш восстанавливается из графа, если чанки туда уже записаны
        (стадия embed прошла), и сразу перезаписывается на диск. Если в графе документа нет,
        исходное исключение чтения кэша (`OSError`, `ValueError`, `KeyError`, `TypeError`)
        пробрасывается."""
        try:
            payload = json.loads(self.chunks_path(doc_id).read_text(encoding="utf-8"))
            return (
                DocumentRecord(**payload["document"]),
                [ChunkRecord(**c) for c in payload["chunks"]],
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            stored = self._graph.doc_chunks(doc_id)
            if stored is None:
                raise
            self._log.warning("chunk_cache_recovered", doc_id=doc_id, error_type=type(exc).__name__)
            self._save_chunks(*stored)
            return stored

    # ---- загрузка ----
    async def ingest(
        self,
        paths: Sequence[Path],
        *,
        build_wiki: bool = True,
        retry_failed: bool = False,
        run_id: str = "",
        progress: ProgressSink = NULL_PROGRESS,
    ) -> IngestReport:
        """Загружает файлы и каталоги `paths` в корпус; вызывается под lock `ingest`.

        Порядок: `prepare()` (проверки индекса, возврат прерванных стадий), доведение
        незавершённых удалений из журнала, обход файлов (`discover`) и `_ingest_file` по
        каждому. Ошибка одного файла не останавливает остальные: она попадает в
        `report.failed`, а статус стадии и файла в registry становится failed. Затем
        предупреждение о смене `extract.entity_types` и, при `build_wiki`, инкрементальная
        сборка wiki с именами удалённых сущностей для `log.md`.

        Бросает `InterruptedResetError` / `EmbedderMismatchError` из `prepare` и
        `PendingRemovalError`, если журнал удалений не удаётся довести."""
        self.prepare()
        report = IngestReport()
        removed_names = self.finish_pending_removals()
        files = self.discover(paths)
        progress.files_found(len(files))
        for path in files:
            key = str(path)
            progress.file_started(key)
            try:
                status = await self._ingest_file(path, retry_failed, removed_names, progress)
            except _PreviouslyFailed as exc:
                report.failed[key] = str(exc)
                status = "failed"
            except Exception as exc:
                report.failed[key] = safe_error(exc)
                self._log.error(
                    "ingest_file_failed",
                    path=key,
                    error_type=type(exc).__name__,
                    error=safe_error(exc),
                    exc_info=True,
                )
                status = "failed"
            else:
                (report.processed if status == "processed" else report.skipped).append(key)
            progress.file_done(status)
        if stale_entity_types(self._registry, self._s.extract.entity_types):
            report.types_warning = TYPES_CHANGED
            self._log.warning("entity_types_changed", detail=TYPES_CHANGED)
        if build_wiki:
            report.wiki = await self._wiki.build(
                run_id=run_id, deleted=removed_names, documents=report.processed, progress=progress
            )
        self._log.info(
            "ingest_done",
            processed=len(report.processed),
            skipped=len(report.skipped),
            failed=len(report.failed),
            wiki_failed=len(report.wiki.failed) if report.wiki else 0,
        )
        return report

    async def remove(
        self,
        paths: Sequence[Path],
        *,
        missing: bool = False,
        run_id: str = "",
        progress: ProgressSink = NULL_PROGRESS,
    ) -> RemovalReport:
        """Забывает зарегистрированные источники, сохраняя исходные файлы на диске.

        Вызывается под `ingest_lock`, как `ingest`. Выбор идёт по registry
        (`select_removals`), поэтому можно передать уже удалённый файл или каталог;
        `missing=True` выбирает строки, файлов которых больше нет.

        Выбранные пути группируются по `doc_id`. Если у содержимого есть живая копия вне
        выбора, знание остаётся: план с `forgotten_paths` журналируется, цитаты
        перенаправляются на копию (`_repoint_doc`), строки путей удаляются, документ попадает
        в `report.retained`. Иначе документ удаляется целиком через журнал
        (`_start_removal`). После всех групп собирается wiki.

        Бросает `PendingRemovalError` при первой неудаче; имена уже удалённых сущностей
        сохраняются в meta, чтобы следующий успешный запуск записал их в `log.md`."""
        rows = select_removals(self._registry, paths, missing=missing)
        # без resume: remove не выполняет стадии, сброс `running` — дело следующего ingest
        self.prepare(resume=False)
        deleted = self.finish_pending_removals()
        report = RemovalReport()
        groups: dict[str, list[str]] = {}
        # строка могла измениться, пока доводились удаления из журнала: берём только те,
        # что всё ещё указывают на то же содержимое
        for row in rows:
            current = self._registry.get_file(row.path)
            if current is not None and current.doc_id == row.doc_id:
                groups.setdefault(row.doc_id, []).append(row.path)
        progress.files_found(sum(map(len, groups.values())))
        for doc_id, selected in groups.items():
            for path in selected:
                progress.file_started(path)
            try:
                survivors = [p for p in self._live_copies(doc_id) if p not in selected]
                if survivors:
                    # журнал и здесь: если перенаправление упадёт, `_resume_removal` увидит
                    # документ в графе и живую копию и доведёт именно перенаправление, а
                    # `forgotten_paths` не даст выбранным путям снова считаться копиями
                    plan = self._removal_plan(doc_id)
                    plan.forgotten_paths = selected
                    self._registry.put_removal(doc_id, plan.model_dump_json())
                    self._repoint_doc(doc_id, survivors)
                    self._forget_paths(doc_id, selected)
                    self._registry.drop_removal(doc_id)
                    report.retained.append(doc_id)
                else:
                    # Ушла последняя живая копия: устаревшие связи тоже должны исчезнуть.
                    # Намерение пишется в журнал раньше, чем меняется любой слой знаний.
                    deleted += self._start_removal(
                        doc_id, forgotten_paths=self._registry.paths_for_doc(doc_id)
                    )
            except Exception as exc:
                self._registry.set_meta(_UNREPORTED_REMOVALS, json.dumps(deleted))
                progress.file_done("failed")
                raise PendingRemovalError(
                    f"не удалось удалить документ {doc_id}: {type(exc).__name__}: {exc}. "
                    "Устраните причину и повторите remove, ingest или wiki build."
                ) from exc
            report.removed.extend(selected)
            for _ in selected:
                progress.file_done("processed")
        report.wiki = await self._wiki.build(run_id=run_id, deleted=deleted, progress=progress)
        self._log.info(
            "remove_done",
            paths=len(report.removed),
            retained_documents=len(report.retained),
            wiki_failed=len(report.wiki.failed),
        )
        return report

    async def _ingest_file(
        self, path: Path, retry_failed: bool, removed_names: list[str], progress: ProgressSink
    ) -> str:
        """Проводит один файл через незавершённые стадии; возвращает "processed" или "skipped".

        Алгоритм:
        1. `doc_id` считается по текущим байтам файла. Если путь раньше указывал на другое
           содержимое (файл изменили), старая версия удаляется (`_remove_doc`), если её не
           держит другая копия.
        2. Если для этого содержимого в журнале висит удаление (упало раньше в этом запуске),
           оно доводится с этим файлом как живой копией.
        3. Нет незавершённых стадий → строка файла `done`, цитаты при необходимости
           перенаправляются на этот путь (`_follow_moved_document`), результат "skipped".
        4. Есть стадия `failed` и нет `retry_failed` → `_PreviouslyFailed`.
        5. Иначе стадии выполняются по порядку с переходами running → done / failed.

        `removed_names` дополняется именами удалённых сущностей (для отчёта wiki). Исключение
        стадии пробрасывается после записи failed в registry."""
        key = str(path)
        doc_id = file_doc_id(path)
        stat = path.stat()
        row = self._registry.get_file(key)
        if row is not None and row.doc_id != doc_id:
            removed_names.extend(self._remove_doc(row.doc_id, keep_path=key))
        journaled = self._registry.get_removal(doc_id)
        if journaled is not None:
            # удаление этого содержимого уже упало в этом запуске: сначала доводим его,
            # считая этот файл живой копией, чтобы содержимое не удалилось и не было
            # пропущено как done, когда его данных уже нет
            plan = _RemovalPlan.model_validate_json(journaled)
            removed_names.extend(self._resume_removal(doc_id, plan, live=[key]))
        pending = self._registry.pending_stages(doc_id)
        if not pending:
            self._registry.upsert_file(key, doc_id, stat.st_size, stat.st_mtime, "done")
            self._follow_moved_document(doc_id, key)
            return "skipped"
        failed_stage = next(
            (s for s in pending if self._registry.stage_status(doc_id, s) == "failed"), None
        )
        if failed_stage and not retry_failed:
            self._registry.upsert_file(key, doc_id, stat.st_size, stat.st_mtime, "failed")
            raise _PreviouslyFailed(
                f"стадия {failed_stage} упала в прошлый раз; запустите с --retry-failed"
            )
        self._registry.upsert_file(key, doc_id, stat.st_size, stat.st_mtime, "processing")
        bind_context(doc_id=doc_id)
        try:
            for stage in pending:
                self._registry.set_stage(doc_id, stage, "running")
                progress.step(stage)
                try:
                    with log_stage(self._log, stage, path=key):
                        await self._run_stage(stage, path, doc_id, progress)
                except Exception as exc:
                    self._registry.set_stage(doc_id, stage, "failed", error=safe_error(exc))
                    self._registry.set_file_status(key, "failed")
                    raise
                self._registry.set_stage(doc_id, stage, "done")
        finally:
            unbind_context("doc_id")
        self._registry.set_file_status(key, "done")
        return "processed"

    def _remove_doc(self, doc_id: str, keep_path: str) -> list[str]:
        """Удаляет старую версию документа, если ни по одному другому пути нет того же
        содержимого; иначе только перенаправляет цитаты на уцелевшую копию.

        `keep_path` — путь, по которому теперь лежит новое содержимое: он не считается копией.
        Если удаление этого `doc_id` уже в журнале, оно доводится. Возвращает имена
        удалённых сущностей."""
        journaled = self._registry.get_removal(doc_id)
        if journaled is not None:  # прерванная прошлая попытка
            return self._resume_removal(doc_id, _RemovalPlan.model_validate_json(journaled))
        # сохраняет его только файл, у которого всё ещё это содержимое (не переименованный
        # и не изменённый)
        survivors = [p for p in self._live_copies(doc_id) if p != keep_path]
        if survivors:
            self._repoint_doc(doc_id, survivors)
            return []
        return self._start_removal(doc_id)

    def finish_pending_removals(self) -> list[str]:
        """Доводит удаления документов, которые прошлый запуск начал, но не завершил (сбой
        или ошибка хранилища); возвращает имена удалённых сущностей.

        Вызывается первым шагом `ingest` / `remove` / `wiki build`. Возвращаемый список
        включает имена из meta `unreported_removed_entities` (удаления, завершённые раньше,
        но не попавшие в отчёт wiki). Записи журнала независимы, поэтому неудача одной не
        останавливает остальные; если неудачи были, имена сохраняются обратно в meta и
        бросается `PendingRemovalError`. При успехе meta-ключ очищается: вызывающий код
        отвечает за то, чтобы имена дошли до отчёта."""
        # имена из удалений, завершённых прошлым вызовом, который затем упал на другом
        names: list[str] = json.loads(self._registry.get_meta(_UNREPORTED_REMOVALS) or "[]")
        failed: dict[str, Exception] = {}
        for doc_id, plan_json in self._registry.pending_removals():
            self._log.warning("resuming_document_removal", old_doc_id=doc_id)
            try:
                names += self._resume_removal(doc_id, _RemovalPlan.model_validate_json(plan_json))
            except Exception as exc:  # остальные независимы: всё равно доводим их
                failed[doc_id] = exc
        if failed:
            # сохраняем для wiki-отчёта / log.md того запуска, который в итоге пройдёт успешно
            self._registry.set_meta(_UNREPORTED_REMOVALS, json.dumps(names))
            causes = "; ".join(f"{d}: {type(e).__name__}: {e}" for d, e in failed.items())
            raise PendingRemovalError(
                f"не удаётся завершить удаление старых версий документов {', '.join(failed)} "
                f"({causes}). Устраните причину (см. `mnogobase doctor`) и запустите команду "
                "снова: сначала повторится удаление, и пока оно не завершится, ничего другого "
                "не выполняется. Начать с нуля можно через `mnogobase reset`."
            ) from next(iter(failed.values()))
        self._registry.set_meta(_UNREPORTED_REMOVALS, "[]")
        return names

    def _start_removal(self, doc_id: str, *, forgotten_paths: Sequence[str] = ()) -> list[str]:
        """Начинает удаление документа: план → журнал → применение.

        План пишется в `pending_removals` до первого изменения хранилищ, так что сбой на
        любом шаге `_apply_removal` будет доведён следующим запуском. Возвращает имена
        удалённых сущностей."""
        plan = self._removal_plan(doc_id)
        plan.forgotten_paths = list(forgotten_paths)
        self._registry.put_removal(doc_id, plan.model_dump_json())
        return self._apply_removal(doc_id, plan)

    def _resume_removal(
        self, doc_id: str, plan: _RemovalPlan, live: Sequence[str] = ()
    ) -> list[str]:
        """Доводит прерванное удаление, заново сверяясь с текущим состоянием.

        Файл с этим содержимым мог появиться снова (другой путь, новый файл, откат): такие
        живые копии не должны лишиться документа. `live` — пути, где оно точно есть.
        Пути из `plan.forgotten_paths` копиями не считаются никогда.

        Ветки: документ ещё в графе (транзакция удаления не закоммитилась) и есть копия →
        удаление отменяется, цитаты перенаправляются на копию; документ в графе без копий →
        план строится заново (`_start_removal`); документа в графе нет → применяется
        сохранённый план, а найденные копии будут заново проиндексированы."""
        survivors = [p for p in self._live_copies(doc_id, live) if p not in plan.forgotten_paths]
        if self._graph.has_document(doc_id):
            # транзакция графа так и не закоммитилась, значит, ещё ничего не удалено
            if survivors:
                self._repoint_doc(doc_id, survivors)
                self._forget_paths(doc_id, plan.forgotten_paths)
                self._registry.drop_removal(doc_id)
                self._log.info("document_removal_dropped", old_doc_id=doc_id, kept_by=survivors)
                return []
            # последующая работа (другие файлы того запуска) могла изменить состав удаления
            return self._start_removal(doc_id, forgotten_paths=plan.forgotten_paths)
        return self._apply_removal(doc_id, plan, survivors)

    def _forget_paths(self, doc_id: str, paths: Sequence[str]) -> None:
        """Удаляет строки `files` для `paths`, но только если путь всё ещё указывает на
        `doc_id`: путь мог уже получить новое содержимое, и его строку трогать нельзя."""
        for path in paths:
            row = self._registry.get_file(path)
            if row is not None and row.doc_id == doc_id:
                self._registry.drop_file(path)

    def _live_copies(self, doc_id: str, live: Sequence[str] = ()) -> list[str]:
        """Пути, файл по которым сейчас с этим содержимым (строки registry могут устареть).

        Кандидаты — пути из registry плюс `live`; каждый проверяется хэшированием файла."""
        candidates = dict.fromkeys([*self._registry.paths_for_doc(doc_id), *live])
        return [p for p in candidates if _holds(p, doc_id)]

    def _removal_plan(self, doc_id: str) -> _RemovalPlan:
        """Только чтение: всё, что должно подчистить удаление, пока это ещё есть в графе.

        Источники: `GraphStore.document_deletion_plan` (сущности, у которых не останется
        упоминаний, их страницы и затронутые сущности) с учётом `doc_entities` из registry;
        страницы, ссылающиеся на удаляемые страницы (`_pages_linking_into`); страницы,
        цитирующие чанки документа. Всё, кроме удаляемого, попадает в `dirty`."""
        linkers = self._pages_linking_into(doc_id)
        touched = self._registry.doc_entities(doc_id)
        result = self._graph.document_deletion_plan(doc_id, touched)
        removed = set(result.removed_entity_ids)
        # страницы, ссылающиеся на удалённую, переписываются, чтобы устаревшая ссылка исчезла
        stale_linkers = {p for target in removed for p in linkers.get(target, [])}
        # страницы, цитирующие чанки документа, даже если MENTIONS этих чанков не записаны
        citing = set(self._graph.pages_citing_document(doc_id))
        return _RemovalPlan(
            removed_entities=dict(
                zip(result.removed_entity_ids, result.removed_names, strict=True)
            ),
            removed_pages=result.removed_pages,
            dirty=sorted((set(result.affected) | stale_linkers | citing) - removed),
            relinked=len(stale_linkers - removed),
            affected_entities=sorted(result.affected),
            touched_entities=touched,
        )

    def _apply_removal(
        self, doc_id: str, plan: _RemovalPlan, survivors: Sequence[str] = ()
    ) -> list[str]:
        """Применяет удаление из журнала: транзакция графа, затем Qdrant, файлы wiki и registry.

        Идемпотентно и безопасно для повтора после другой работы: после коммита транзакции
        графа источник истины — граф. Сущность или страница, которая есть в плане, но снова
        есть в графе (заново упомянута или пересоздана с тем же id более поздним файлом), —
        это живые данные: её point, секции и файл остаются, и она перегенерируется (помечается
        dirty). `survivors` — живые копии содержимого, найденные после коммита транзакции
        графа: их данных уже нет, поэтому их стадии сбрасываются (кэши сохраняются), и они
        ingest-ятся заново, а не остаются "done".
        Возвращает имена сущностей, которые действительно удалены.

        Шаги (в таком порядке): транзакция графа → dirty-очередь → точки Qdrant (чанки,
        сущности, секции страниц) → стирание объединённых описаний затронутых сущностей
        (пересчёт их векторов) → инвалидация сгенерированных страниц → файлы удалённых
        страниц → стадии / кэши документа в registry и на диске → строки забытых путей →
        `drop_removal`. Сеть: Neo4j, Qdrant и embedder; ошибки любого из них пробрасываются,
        а запись журнала остаётся для повтора."""
        # одна транзакция; после коммита — no-op
        self._graph.delete_document(doc_id, plan.touched_entities)
        gone = {e: n for e, n in plan.removed_entities.items() if self._graph.get_entity(e) is None}
        revived = set(plan.removed_entities) - set(gone)
        # dirty пишется раньше любых удалений из Qdrant и с диска: сбой дальше не потеряет
        # того, что страницы нужно перегенерировать
        self._registry.mark_dirty(set(plan.dirty) | revived)
        self._registry.clear_dirty(gone)
        self._vectors.delete_doc(doc_id)
        self._vectors.delete_entities(sorted(gone))
        for eid in gone:
            # секции страницы, которая так и не попала в граф (сбой между Qdrant и Neo4j)
            self._vectors.delete_wiki_page(eid)
        # У объединённых описаний нет привязки к источникам: сущности остаются, но описания,
        # которые могли опираться на удалённый источник, стираются в обоих хранилищах
        # (вектор считается до записи, затем Qdrant, затем Neo4j — обычный порядок записи).
        affected = list(plan.affected_entities)
        for start in range(0, len(affected), ENTITY_BATCH):
            records = [
                record.model_copy(update={"description": "", "descriptions": []})
                for record in self._graph.get_entities(affected[start : start + ENTITY_BATCH])
            ]
            if records:
                vectors = self._embedder.embed_documents([entity_embed_input(r) for r in records])
                self._vectors.upsert_entities(records, vectors)
                self._graph.upsert_entities(records)
        # Сгенерированные страницы инвалидируются до вызовов LLM: упавшая пересборка не должна
        # оставить удалённые утверждения в поиске, а старый текст не должен стать основой
        # следующей генерации (LLM обновляет существующую страницу).
        for eid in sorted(set(plan.dirty) | revived):
            page = self._graph.wiki_page(eid)
            if page is not None:
                # узел остаётся (slug, входящие ссылки), но индекс, reindex и API его не видят
                self._graph.invalidate_wiki_page(page.page_id)
                self._vectors.delete_wiki_page(page.page_id)
                (self._s.wiki.dir / page.path).unlink(missing_ok=True)
        # освободившийся slug может уже принадлежать странице другой сущности, записанной позже
        live_paths = (
            {row.path for row in self._graph.wiki_pages(include_stale=True)}
            if plan.removed_pages
            else set()
        )
        for page_id, rel_path in plan.removed_pages:
            if self._graph.wiki_page(page_id) is None:
                self._vectors.delete_wiki_page(page_id)
            if rel_path not in live_paths:
                (self._s.wiki.dir / rel_path).unlink(missing_ok=True)
        if survivors:
            # данных копий в хранилищах уже нет: стадии заново, кэши парсинга / чанков /
            # извлечения остаются, так что повтор дешёвый
            self._registry.reset_stages(doc_id)
            for path in survivors:
                self._registry.set_file_status(path, "pending")
            self._log.warning("document_needs_reingest", doc_id=doc_id, paths=list(survivors))
        else:
            self._registry.clear_doc(doc_id)
            self._parser.drop_cache(doc_id)
            self.chunks_path(doc_id).unlink(missing_ok=True)
        self._forget_paths(doc_id, plan.forgotten_paths)
        # последним: до этого момента любой сбой оставляет запись журнала для повтора
        self._registry.drop_removal(doc_id)
        self._log.info(
            "document_removed",
            old_doc_id=doc_id,
            removed_entities=len(gone),
            revived_entities=len(revived),
            relinked_pages=plan.relinked,
        )
        return sorted(gone.values())

    def _pages_linking_into(self, doc_id: str) -> dict[str, list[str]]:
        """Перед каскадным удалением: для каждой wiki-страницы сущности, упомянутой в этом
        документе, — страницы, ссылающиеся на неё (page_id == entity_id). Рёбра исчезнут
        вместе с удалением."""
        mentioned = {e for ids in self._graph.chunk_entity_ids(doc_id).values() for e in ids}
        paged = [
            row.page_id
            for row in self._graph.wiki_pages(include_stale=True)
            if row.page_id in mentioned
        ]
        return {page_id: self._graph.pages_linking_to([page_id]) for page_id in paged}

    def _follow_moved_document(self, doc_id: str, path: str) -> None:
        """По `path` лежит уже загруженный документ: если по пути из его цитат документа
        больше нет (файл переименовали, удалили или изменили), перенаправляет их на `path`.
        Затем забывает строки registry этого содержимого, чьих файлов уже нет, чтобы
        следующим запускам не проверять их снова."""
        if all(p == path for p in self._registry.paths_for_doc(doc_id)):
            return  # у других файлов этого содержимого не было: цитаты уже указывают на `path`
        if any(p != path and not _holds(p, doc_id) for p in self._recorded_paths(doc_id)):
            self._repoint_doc(doc_id, [path])
        for stale in self._registry.paths_for_doc(doc_id):
            if not Path(stale).exists():
                self._registry.drop_file(stale)

    def _recorded_paths(self, doc_id: str) -> set[str]:
        """Пути, указанные в сохранённом документе: в кэше чанков и в узле Document графа
        (любого может не быть: кэш удалили или документ так и не заэмбеддили)."""
        recorded = set()
        if self.chunks_path(doc_id).exists():
            recorded.add(self.load_chunks(doc_id)[0].path)
        graph_path = self._graph.document_path(doc_id)
        if graph_path is not None:
            recorded.add(graph_path)
        return recorded

    def _repoint_doc(self, doc_id: str, survivors: list[str]) -> None:
        """Общее содержимое остаётся; следит, чтобы цитаты указывали на путь, где оно ещё есть.

        Qdrant обновляется первым, кэш чанков — последним: следующий запуск решает по кэшу и
        графу, так что сбой на полпути будет повторён, а не забыт."""
        recorded = self._recorded_paths(doc_id)
        if not recorded:
            return  # чанков не было: путь ещё нигде не сохранён
        if recorded <= set(survivors):
            return
        path = survivors[0]
        self._log.info("document_repointed", old_path=sorted(recorded - {path}), path=path)
        # Инвалидация wiki сохраняется до записи путей: сервер может закоммитить запись и
        # всё равно вернуть ошибку, и тогда повтор уже не найдёт старого пути, по которому
        # понял бы, что страницы нужно перегенерировать.
        self._registry.mark_dirty(self._graph.pages_citing_document(doc_id))
        self._vectors.set_doc_path(doc_id, path)
        self._graph.set_document_path(doc_id, path)
        if self.chunks_path(doc_id).exists():
            doc, chunks = self.load_chunks(doc_id)
            doc = doc.model_copy(update={"path": path})
            self._save_chunks(doc, [c.model_copy(update={"path": path}) for c in chunks])

    async def _run_stage(
        self, stage: str, path: Path, doc_id: str, progress: ProgressSink = NULL_PROGRESS
    ) -> None:
        """Выполняет одну стадию документа; статус стадии ведёт вызывающий `_ingest_file`.

        Стадии после `parse` читают результат предыдущей из кэша (парсера или чанков), а не
        из памяти, поэтому любую можно продолжить в новом процессе. Неизвестное имя стадии —
        `ValueError`."""
        if stage == "parse":
            self._parser.parse(path, doc_id)
        elif stage == "chunk":
            parsed = self._parser.load(doc_id, path)
            chunks = self._chunker.chunk(parsed, doc_id, str(path))
            doc = DocumentRecord(
                doc_id=doc_id,
                path=str(path),
                title=parsed.title,
                mime=parsed.mime,
                n_pages=parsed.n_pages,
            )
            self._save_chunks(doc, chunks)
        elif stage == "embed":
            self._embed(doc_id)
        elif stage == "extract":
            await self._extract(doc_id, progress)
        elif stage == "graph":
            await self._build_graph(doc_id, progress)
        else:
            raise ValueError(f"неизвестная стадия {stage}")

    def index_chunks(self, doc: DocumentRecord, chunks: list[ChunkRecord]) -> None:
        """Эмбеддит чанки (dense + sparse) и делает их upsert в Qdrant.

        Эмбеддится `context_text` чанка (с контекстом заголовков), к dense-входу добавляется
        заголовок документа для шаблона embedder. Используется стадией `embed` и `reindex`.
        Граф не трогает."""
        if not chunks:
            return
        texts = [c.context_text for c in chunks]
        dense = self._embedder.embed_documents([EmbedInput(text=t, title=doc.title) for t in texts])
        self._vectors.upsert_chunks(chunks, dense, self._sparse.encode_documents(texts))

    def _embed(self, doc_id: str) -> None:
        """Стадия `embed`: векторы чанков в Qdrant, затем узлы `Document` и `Chunk` в Neo4j.

        Узел `Document` пишется и для документа без чанков, чтобы удаление и
        `_recorded_paths` его видели."""
        doc, chunks = self.load_chunks(doc_id)
        # Qdrant раньше Neo4j: упавший embedder или Qdrant не оставит граф впереди векторов
        self.index_chunks(doc, chunks)
        self._graph.upsert_document(doc)
        if not chunks:
            return
        self._graph.upsert_chunks(chunks)

    async def _extract(self, doc_id: str, progress: ProgressSink) -> None:
        """Стадия `extract`: LLM-извлечение сущностей и связей для всех чанков.

        Результаты ложатся в `extraction_cache` (это делает `Extractor`), уже закэшированные
        чанки LLM не вызывают. Отдельные чанки могут упасть: если их доля больше
        `extract.max_failed_ratio`, бросается `ExtractionFailedError`, иначе только
        предупреждение в логе, а стадия `graph` пропустит такие чанки."""
        doc, chunks = self.load_chunks(doc_id)
        if not chunks:
            return
        progress.step("extract", len(chunks))
        results = await self._extractor.extract_many(chunks, doc.title, progress)
        failed = len(chunks) - len(results)
        if failed / len(chunks) > self._s.extract.max_failed_ratio:
            raise ExtractionFailedError(f"извлечение не удалось для {failed}/{len(chunks)} чанков")
        if failed:
            self._log.warning("extract_partial", failed=failed, total=len(chunks))

    async def _build_graph(self, doc_id: str, progress: ProgressSink) -> None:
        """Стадия `graph`: извлечения из кэша → разрешённые сущности → факты чанков в графе.

        Для каждого чанка: каждая извлечённая сущность проходит `EntityResolver.resolve`
        (слияние с существующей или создание; resolver сам пишет сущность в Qdrant и Neo4j)
        и сразу записывается в `doc_entities`; имена и алиасы сопоставляются с `entity_id`,
        связи между разными известными сущностями собираются в список; затем id сущностей
        пишутся в payload чанка в Qdrant, а MENTIONS + RELATED — в Neo4j одной транзакцией.
        В конце все затронутые сущности попадают в `dirty_entities` для сборки wiki.

        Если доля чанков без кэша извлечения больше `extract.max_failed_ratio`, стадия
        `extract` возвращается в `pending` и бросается `ExtractionFailedError`."""
        # кэш сопоставлений resolver — только на один документ: между документами сущности
        # могли быть удалены и пересозданы, старое сопоставление увело бы не туда
        self._resolver.forget()
        _doc, chunks = self.load_chunks(doc_id)
        # стадия extract могла отработать с другой моделью / версией промпта (их сменили
        # перед повтором этой стадии): её результаты всё равно — знания этого документа
        results = {c.chunk_id: self._extractor.cached(c, any_version=True) for c in chunks}
        missing = sum(1 for r in results.values() if r is None)
        if chunks and missing / len(chunks) > self._s.extract.max_failed_ratio:
            # никогда не помечаем graph как done без знаний; повтор сначала перезапустит extract
            self._registry.set_stage(doc_id, "extract", "pending")
            raise ExtractionFailedError(
                f"у {missing}/{len(chunks)} чанков нет извлечения в кэше; "
                "запустите с --retry-failed, чтобы извлечь их заново"
            )
        progress.step("graph", sum(len(r.entities) for r in results.values() if r is not None))
        touched: set[str] = set()
        for chunk in chunks:
            result = results[chunk.chunk_id]
            if result is None:
                continue
            ids_by_name: dict[str, str] = {}
            for extracted in result.entities:
                record = await self._resolver.resolve(extracted)
                # resolver уже записал сущность в Qdrant и Neo4j, а MENTIONS появятся только
                # после всего чанка: запоминаем её сразу, чтобы удаление документа её нашло
                self._registry.add_doc_entity(doc_id, record.entity_id)
                progress.advance()
                ids_by_name[normalize_name(extracted.name)] = record.entity_id
                # основное имя важнее алиаса: алиас не перебивает уже известное имя
                for alias in extracted.aliases:
                    ids_by_name.setdefault(normalize_name(alias), record.entity_id)
            entity_ids = sorted(set(ids_by_name.values()))
            relations = []
            for rel in result.relations:
                src = ids_by_name.get(normalize_name(rel.source))
                dst = ids_by_name.get(normalize_name(rel.target))
                # связь с сущностью, которой нет среди сущностей чанка, или петля — отбрасывается
                if src and dst and src != dst:
                    relations.append(
                        {
                            "src": src,
                            "dst": dst,
                            "predicate": rel.predicate,
                            "description": rel.description,
                            "strength": rel.strength,
                        }
                    )
            # порядок записи: Qdrant, затем Neo4j (одна транзакция на чанк)
            self._vectors.set_chunk_entities(chunk.chunk_id, entity_ids)
            self._graph.add_chunk_facts(chunk.chunk_id, entity_ids, relations)
            touched.update(entity_ids)
        self._registry.mark_dirty(touched)
