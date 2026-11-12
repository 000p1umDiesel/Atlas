"""Команды, которые меняют корпус: `ingest`, `remove` и `wiki build`.

Все три пишут в Qdrant, Neo4j и registry, поэтому держат блокировку писателя корпуса
(`common.corpus_lock`) на всё время работы: параллельно с ними не идут ни другой CLI-писатель,
ни фоновый worker.

Порядок подготовки у всех одинаковый: настройки → проверка сервисов (`preflight`) →
блокировка → `App`. `App` закрывается во внутреннем `finally`, блокировка отпускается во
внешнем, так что ни соединения, ни блокировка не остаются висеть после ошибки. Удаления,
прерванные сбоем, записаны в журнал `pending_removals` и доделываются первыми.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from mnogobase.cli import common, render
from mnogobase.core.database import database_target
from mnogobase.core.log import new_run_id
from mnogobase.ingest.pipeline import EmbedderMismatchError, PendingRemovalError
from mnogobase.ingest.processing import measured_ingest
from mnogobase.ingest.removal import select_removals
from mnogobase.stores.qdrant_store import DimensionMismatchError
from mnogobase.stores.registry import Registry

app = common.app
wiki_app = typer.Typer(no_args_is_help=True, help="Команды wiki")
app.add_typer(wiki_app, name="wiki")


@app.command()
def ingest(
    paths: Annotated[list[Path] | None, typer.Argument(help="Файлы или папки для загрузки")] = None,
    no_wiki: Annotated[bool, typer.Option("--no-wiki", help="Не обновлять wiki")] = False,
    retry_failed: Annotated[
        bool, typer.Option("--retry-failed", help="Повторить упавшие стадии")
    ] = False,
) -> None:
    """Загрузить документы в базу знаний и обновить wiki.

    Каждый файл проходит стадии parse, chunk, embed, extract и graph; неизменённые файлы
    пропускаются, упавшие стадии повторяются только с --retry-failed (без путей это
    повторяет все файлы с упавшими стадиями). Код выхода 1, если не обработался какой-то
    файл или страница wiki; 2, если недоступен сервис, индекс построен другим эмбеддером
    или уже работает другой писатель (ingest, remove, worker и т. п.)."""
    console = common.console
    settings = common.get_settings()
    common.preflight(settings, common.INGEST_CHECKS)
    lock = common.corpus_lock(settings)
    try:
        application = common.build_app(settings)
        try:
            targets = list(paths or [])
            if not targets and retry_failed:
                # без путей `--retry-failed` повторяет все файлы с упавшими стадиями
                targets = [Path(p) for p in application.registry.failed_paths()]
            if not targets:
                console.print("Нечего загружать.")
                return
            run_id = new_run_id()
            with render.progress() as progress:
                try:
                    report = asyncio.run(
                        measured_ingest(
                            application,
                            targets,
                            settings=settings,
                            build_wiki=not no_wiki,
                            retry_failed=retry_failed,
                            run_id=run_id,
                            progress=progress,
                        )
                    )
                except (EmbedderMismatchError, DimensionMismatchError, PendingRemovalError) as exc:
                    common.fail(exc)
        finally:
            application.close()
    finally:
        lock.release()
    console.print(
        f"запуск {run_id}: обработано {len(report.processed)}, пропущено {len(report.skipped)}, "
        f"с ошибкой {len(report.failed)}"
    )
    if report.wiki:
        render.print_wiki_report(report.wiki)
    if report.types_warning:
        console.print(
            f"[yellow]предупреждение:[/yellow] {escape(report.types_warning)}", soft_wrap=True
        )
    for path, error in report.failed.items():
        console.print(f"[red]✗[/red] {escape(path)}: {escape(error)}")
    if report.failed or (report.wiki and report.wiki.failed):
        raise typer.Exit(1)


@app.command()
def remove(
    paths: Annotated[
        list[Path] | None,
        typer.Argument(
            help="Зарегистрированные файлы или папки источников; файлы на диске сохраняются"
        ),
    ] = None,
    missing: Annotated[
        bool,
        typer.Option(
            "--missing", help="Забыть только пропавшие источники (при необходимости внутри PATH)"
        ),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Показать подходящие источники, ничего не удаляя")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Не спрашивать подтверждение")] = False,
) -> None:
    """Забыть источники: удалить их знания из индексов, графа и wiki.

    Сами файлы на диске не трогаются. Команда показывает выбранные источники и просит
    подтверждения (--yes его пропускает, --dry-run только показывает список). Заодно
    доделываются прерванные удаления и отложенные обновления wiki. Код выхода 1 при отказе
    от подтверждения или если часть страниц wiki не пересобралась; 2 при ошибке пути,
    недоступном сервисе или занятой блокировке."""
    console = common.console
    settings = common.get_settings(project=True)
    lock = common.corpus_lock(settings)
    try:
        registry = Registry(database_target(settings))
        try:
            selected = select_removals(registry, list(paths or []), missing=missing)
            # незавершённые удаления и страницы wiki в очереди дорабатываются и без новых путей
            recovery = bool(registry.pending_removals() or registry.dirty())
        finally:
            registry.close()
        if not selected and not recovery:
            console.print("Подходящих зарегистрированных источников нет.")
            return
        console.print(f"Выбрано источников: {len(selected)} (сами файлы сохраняются)")
        for row in selected:
            console.print(f"  {escape(row.path)}", soft_wrap=True)
        if recovery:
            console.print("Заодно будут доделаны незавершённые удаления и обновления wiki.")
        if dry_run:
            return
        if not yes and not typer.confirm("Удалить эти знания и обновить wiki?", default=False):
            raise typer.Exit(1)
        common.preflight(settings)
        application = common.build_app(settings)
        try:
            run_id = new_run_id()
            with render.progress() as progress:
                report = asyncio.run(
                    application.pipeline.remove(
                        list(paths or []), missing=missing, run_id=run_id, progress=progress
                    )
                )
        finally:
            application.close()
    except (
        OSError,
        ValueError,
        PendingRemovalError,
        EmbedderMismatchError,
        DimensionMismatchError,
    ) as exc:
        common.fail(exc)
    finally:
        lock.release()
    console.print(
        f"запуск {run_id}: удалено путей источников: {len(report.removed)}, "
        f"сохранено документов с другой копией: {len(report.retained)}"
    )
    if report.wiki:
        render.print_wiki_report(report.wiki)
        if report.wiki.failed:
            console.print("Оставшиеся страницы можно повторить командой `mnogobase wiki build`.")
            raise typer.Exit(1)


@wiki_app.command("build")
def wiki_build(
    rebuild_all: Annotated[bool, typer.Option("--all", help="Пересобрать все страницы")] = False,
) -> None:
    """Пересобрать страницы wiki для сущностей с новыми упоминаниями.

    С --all перегенерируются все страницы. Перед сборкой доделываются прерванные удаления
    документов. Код выхода 1, если часть страниц не собралась (их повторит следующая
    сборка); 2, если недоступен сервис или уже работает другой писатель."""
    settings = common.get_settings(project=True)
    common.preflight(settings)
    lock = common.corpus_lock(settings)
    try:
        application = common.build_app(settings)
        try:
            try:
                application.pipeline.prepare(resume=False)
            except (EmbedderMismatchError, DimensionMismatchError) as exc:
                common.fail(exc)
            # прерванный ingest мог оставить удаление документа недоделанным (страницы-сироты);
            # `deleted` — имена удалённых при этом сущностей, они попадут в отчёт wiki и log.md
            try:
                deleted = application.pipeline.finish_pending_removals()
            except PendingRemovalError as exc:
                common.fail(exc)
            with render.progress() as progress:
                report = asyncio.run(
                    application.wiki.build(
                        rebuild_all=rebuild_all,
                        run_id=new_run_id(),
                        deleted=deleted,
                        progress=progress,
                    )
                )
        finally:
            application.close()
    finally:
        lock.release()
    render.print_wiki_report(report)
    if report.failed:
        raise typer.Exit(1)
