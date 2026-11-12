"""Команды состояния проекта: `doctor`, `status`, `reindex`, `reset`.

`doctor` и `status` только читают и блокировок не берут. `reindex` и `reset` переписывают
корпус целиком и поэтому держат блокировку писателя корпуса (`common.corpus_lock`): пока они
работают, ни ingest, ни worker не запустятся.
"""

from __future__ import annotations

from collections import Counter
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from mnogobase.cli import common, render
from mnogobase.core.config import Settings
from mnogobase.core.database import database_target, project_exists
from mnogobase.extraction.extractor import TYPES_CHANGED, stale_entity_types
from mnogobase.ops.maintenance import ReindexError
from mnogobase.ops.maintenance import reindex as do_reindex
from mnogobase.ops.maintenance import reset as do_reset
from mnogobase.stores.registry import STAGES, Registry

app = common.app


@app.command()
def doctor() -> None:
    """Проверить окружение проекта: модели, LLM, базы данных и индекс.

    Проверяются device, серверы моделей, токенизатор, LLM endpoint, Qdrant, Neo4j,
    совместимость индекса с текущим эмбеддером и типы сущностей. Статус warn — предупреждение,
    а не ошибка. Код выхода 1, если хотя бы одна проверка не прошла."""
    checks = common.run_checks(common.get_settings())
    table = Table("check", "status", "detail")
    for c in checks:
        table.add_row(c.name, render.check_status(c), escape(c.detail))
    common.console.print(table)
    if not all(c.ok for c in checks):
        raise typer.Exit(1)


@app.command()
def status() -> None:
    """Показать состояние проекта: файлы, стадии, очередь wiki и ошибки.

    Печатает число файлов по статусам, таблицу стадий конвейера, сущности, ждущие
    обновления wiki, незавершённые удаления документов и последние ошибки стадий. Вне
    проекта сообщает об этом и завершается с кодом 0, ничего не создавая."""
    console = common.console
    # status только читает: вне проекта он сообщает об этом и завершается успешно
    settings = common.get_settings(project=True, missing_exit=0)
    registry = Registry(database_target(settings))
    try:
        files = registry.files()
        counts = Counter(f.status for f in files)
        console.print(
            f"files: {len(files)} " + " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        )
        summary = registry.stage_summary()
        table = Table("stage", "done", "failed", "pending", "running")
        for stage in STAGES:
            row = summary.get(stage, {})
            table.add_row(
                stage, *(str(row.get(k, 0)) for k in ("done", "failed", "pending", "running"))
            )
        console.print(table)
        console.print(f"entities waiting for wiki update: {len(registry.dirty())}")
        removals = [doc_id for doc_id, _ in registry.pending_removals()]
        console.print(f"pending document removals: {len(removals)}")
        if removals:
            console.print(
                f"  unfinished: {', '.join(removals)} — the next ingest / remove / wiki build retries "
                "them first",
                soft_wrap=True,
            )
        if stale_entity_types(registry, settings.extract.entity_types):
            console.print(f"[yellow]warning:[/yellow] {escape(TYPES_CHANGED)}", soft_wrap=True)
        errors = registry.stage_errors()
        if errors:
            err_table = Table("doc_id", "stage", "error")
            for doc_id, stage, error in errors:
                err_table.add_row(doc_id, stage, escape(error or ""))
            console.print(err_table)
    finally:
        registry.close()


@app.command()
def reindex() -> None:
    """Пересчитать все векторы после смены эмбеддера (граф и wiki сохраняются).

    Нужна, когда меняются модель, размерность, шаблоны, backend или квантование
    эмбеддера: до этого ingest и ask отказываются работать с индексом. Коллекции Qdrant
    пересоздаются для чанков, сущностей и разделов wiki; LLM не вызывается."""
    settings = common.get_settings(project=True)
    # reindex пересоздаёт коллекции: смена размерности — ровно то, для чего он нужен
    common.preflight(settings, common.REINDEX_CHECKS, check_dim=False)
    lock = common.corpus_lock(settings)
    try:
        application = common.build_app(settings)
        try:
            with render.progress() as progress:
                try:
                    stats = do_reindex(application, progress=progress)
                except ReindexError as exc:
                    common.fail(exc)
        finally:
            application.close()
    finally:
        lock.release()
    common.console.print(
        f"reindexed: {stats['chunks']} chunks, {stats['entities']} entities, "
        f"{stats['wiki_sections']} wiki sections"
    )


def _reset_targets(settings: Settings) -> list[str]:
    """Что именно удалит `reset`: список строк, который показывается пользователю перед
    подтверждением. Пути разрешаются в абсолютные, чтобы было видно, какой проект стирается."""
    wiki_dir = settings.wiki.dir.resolve()
    return [
        f"Neo4j {settings.neo4j.uri}: all nodes in the default database",
        f"Qdrant {settings.qdrant.url}: collections with prefix {settings.qdrant.prefix!r}",
        f"registry in {database_target(settings)}; file caches in {settings.data_dir.resolve()}",
        f"wiki pages in {wiki_dir}: entities/, index.md, log.md",
    ]


@app.command()
def reset(
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask for confirmation.")] = False,
) -> None:
    """Удалить ВСЕ векторы, данные графа, состояние registry, кэши и страницы wiki.

    Стираются все узлы базы Neo4j, коллекции Qdrant с префиксом проекта, registry и история
    очереди в PostgreSQL, кэши в каталоге данных и сгенерированные страницы wiki; исходные
    документы остаются. Перед удалением печатается список и запрашивается подтверждение
    (--yes его пропускает). Вне существующего проекта команда отказывается даже с --yes.
    Код выхода 1 при отказе от подтверждения, 2 вне проекта или при занятой блокировке."""
    console = common.console
    settings = common.get_settings()
    if not project_exists(settings):
        # --yes это не обходит: неверный CWD или конфиг не должен стереть чужие данные
        console.print(
            f"[red]No mnogobase project here: {escape(str(database_target(settings)))} "
            "does not exist.[/red] Run reset from the project directory or pass its --config.",
            soft_wrap=True,
        )
        raise typer.Exit(2)
    common.preflight(settings, common.RESET_CHECKS)
    console.print("This deletes:", soft_wrap=True)
    for target in _reset_targets(settings):
        console.print(f"  - {escape(target)}", soft_wrap=True)
    if not yes and not typer.confirm("Delete all of the above?"):
        raise typer.Exit(1)
    lock = common.corpus_lock(settings)
    try:
        application = common.build_app(settings)
        try:
            do_reset(application)
        finally:
            application.close()
    finally:
        lock.release()
    console.print("All data deleted.")
