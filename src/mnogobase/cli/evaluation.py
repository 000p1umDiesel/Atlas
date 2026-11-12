"""Команды оценки качества retrieval (`eval run | score | report | ingest | benchmark`).

Сама логика — в пакете `mnogobase.evaluation` (план, ответы, судья, отчёты); здесь только
команды, блокировки и прогресс. При импорте `App` не создаётся и Ragas не загружается.

Каталог эксперимента (`--output`) защищён файловой блокировкой `.eval.lock`: в один каталог
одновременно пишет только один процесс, второй сразу получает отказ (код 2). Кроме того:
- `eval run` на всё время генерации ответов держит блокировку корпуса (`ingest`): корпус не
  должен меняться посреди прогона, иначе ответы разных режимов относились бы к разным
  данным;
- `eval ingest` загружает документы под той же блокировкой и записывает замеры затрат в
  `processing.jsonl` каталога эксперимента;
- `eval score` и `eval report` работают только с сохранёнными артефактами и сервисов
  retrieval не трогают.

Все команды возобновляемы: повторный запуск с тем же каталогом доделывает только то, что
ещё не выполнено успешно.
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack, contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import filelock
import typer
from rich.markup import escape
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from mnogobase.cli import common, render
from mnogobase.core.database import LockBusyError, database_target
from mnogobase.evaluation.core import (
    load_questions,
    load_run,
    prepare_run,
    report,
    run_answers,
    score_run,
)
from mnogobase.evaluation.ragas import judge_config
from mnogobase.ingest.processing import corpus_snapshot, measured_ingest
from mnogobase.retrieval import Mode
from mnogobase.stores.registry import ingest_lock

app = typer.Typer(no_args_is_help=True, help="Сравнение режимов retrieval по метрикам Ragas")
common.app.add_typer(app, name="eval")


@contextmanager
def _lock(output: Path):
    """Неблокирующая файловая блокировка каталога эксперимента; создаёт каталог.

    Занята — сразу `filelock.Timeout` (команды превращают его в код 2). Файловая, а не
    PostgreSQL: каталог — локальные файлы, и `eval report` работает без базы."""
    output.mkdir(parents=True, exist_ok=True)
    with filelock.FileLock(output / ".eval.lock", timeout=0):
        yield


@contextmanager
def _progress(console):
    """Прогресс одной фазы оценки (ответы или судья); отдаёт колбэк `update(event, **fields)`.

    События от `evaluation.core`: `start` (total, remaining — сколько всего и сколько
    осталось после прошлых запусков), `item` (label текущей пары) и `done` (status, label,
    detail). Неудачи (`error`, `undefined`) печатаются сразу, итог — после фазы."""
    bars = Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        TextColumn("{task.completed:.0f}/{task.total:.0f}"),
        TimeElapsedColumn(),
        console=console,
        disable=not console.is_terminal,
    )
    task = None
    failed = 0

    def update(event, **fields):
        nonlocal task, failed
        if event == "start":
            total, remaining = fields["total"], fields["remaining"]
            console.print(
                f"Запланировано: {total}; выполнено: {total - remaining}; осталось: {remaining}"
            )
            task = bars.add_task("подготовка", total=total, completed=total - remaining)
        elif event == "item":
            bars.update(task, description=escape(fields["label"]))
        elif event == "done":
            if fields["status"] in ("error", "undefined"):
                failed += 1
                console.print(f"[red]{escape(fields['label'])}: {escape(fields['detail'])}[/red]")
            bars.update(task, advance=1)

    with bars:
        yield update
    console.print(f"Фаза завершена; попыток с ошибкой или без оценки: {failed}")


def _print_summary(output: Path, console):
    """Пересобирает отчёты по сохранённым артефактам, печатает сводную таблицу и
    возвращает её строки (список словарей из `summary.csv`)."""
    summary = report(output)
    table = Table("подход", "метрика", "среднее", "ok/план", "пропущено", "error/undefined/missing")
    for row in summary:
        table.add_row(
            row["approach"],
            row["metric"],
            f"{row['mean']:.4f}" if row["mean"] is not None else "—",
            f"{row['ok']}/{row['planned']}",
            str(row["skipped"]),
            f"{row['error']}/{row['undefined']}/{row['missing']}",
        )
    console.print(table)
    console.print(f"Отчёты: {escape(str(output / 'report.md'))}, details.csv, summary.csv")
    return summary


@app.command()
def run(
    dataset: Annotated[
        Path, typer.Argument(help="Вопросы в JSONL: id, question и необязательный reference")
    ],
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Новый каталог запуска или каталог с тем же планом"),
    ],
    mode: Annotated[
        list[Mode] | None,
        typer.Option(
            "--mode", "-m", help="Режим; повторите для нескольких (по умолчанию rag, wiki, graph)"
        ),
    ] = None,
    limit: Annotated[
        int | None, typer.Option("--limit", min=1, help="Взять только первые N вопросов")
    ] = None,
    k: Annotated[
        int | None, typer.Option("--k", min=1, help="Сколько результатов берёт каждый retriever")
    ] = None,
) -> None:
    """Сгенерировать и сохранить ответы на вопросы набора в выбранных режимах.

    Первый запуск фиксирует план эксперимента в run.json (вопросы, режимы, k, настройки,
    промпт и снимок корпуса); повторный запуск в тот же каталог доделывает только
    неудачные и недостающие ответы, а при другом плане отказывается. Пока идут ответы,
    корпус заблокирован для записи. Код выхода 1, если часть ответов не получилась; 2 при
    неверном наборе, недоступном сервисе или занятом каталоге либо корпусе."""
    try:
        load_questions(dataset)  # битый набор отклоняется до загрузки конфига и сервисов
        settings = common.get_settings(project=True)
        with _lock(output), ingest_lock(database_target(settings)):
            common.preflight(settings)
            application = common.build_app(settings)
            try:
                retrievers, answerer, translator = common.answer_setup(application)
                prepare_run(
                    dataset,
                    output,
                    settings,
                    modes=[m.value for m in mode] if mode else None,
                    limit=limit,
                    k=k,
                    corpus=corpus_snapshot(settings),
                )
                with _progress(common.console) as progress:
                    failed = asyncio.run(
                        run_answers(output, retrievers, answerer, translator, progress=progress)
                    )
            finally:
                application.close()
            report(output)
    except (OSError, ValueError, filelock.Timeout, LockBusyError) as exc:
        common.fail(exc)
    common.console.print(
        f"Ответы сохранены в {escape(str(output))}; с ошибкой: {failed}. "
        f"Дальше: `mnogobase eval score {escape(str(output))}`"
    )
    if failed:
        raise typer.Exit(1)


@app.command()
def score(
    output: Annotated[Path, typer.Argument(help="Каталог сохранённого запуска")],
    metric: Annotated[
        list[str] | None,
        typer.Option("--metric", help="Метрика; повторите для нескольких (по умолчанию все пять)"),
    ] = None,
) -> None:
    """Оценить сохранённые ответы метриками Ragas с LLM-судьёй.

    Сервисы retrieval не нужны, только судья (evaluation.* или, по умолчанию, llm и
    embedder проекта). Уже посчитанные оценки тех же ответов не пересчитываются. После
    оценки пересобираются отчёты. Код выхода 1, если остались неоценённые пары или
    ответы с ошибками."""
    try:
        load_run(output)
        settings = common.get_settings()
        with _lock(output):
            with _progress(common.console) as progress:
                failed = asyncio.run(
                    score_run(output, judge_config(settings), names=metric, progress=progress)
                )
            summary = _print_summary(output, common.console)
    except (OSError, ValueError, filelock.Timeout, LockBusyError) as exc:
        common.fail(exc)
    if failed or any(s["answers_error"] or s["answers_missing"] for s in summary):
        raise typer.Exit(1)


@app.command(name="report")
def report_command(
    output: Annotated[Path, typer.Argument(help="Каталог сохранённого запуска")],
) -> None:
    """Пересобрать отчёты по сохранённым ответам и оценкам.

    Пишет report.md, summary.csv, details.csv и файлы сравнения; модели и сервисы не
    вызываются."""
    try:
        load_run(output)
        with _lock(output):
            _print_summary(output, common.console)
    except (OSError, ValueError, filelock.Timeout, LockBusyError) as exc:
        common.fail(exc)


class Phase(StrEnum):
    """Метка замера в `eval ingest`: первичная сборка корпуса или его последующее
    обновление. Отчёт считает затраты этих фаз отдельно."""

    BUILD = "build"
    UPDATE = "update"


@app.command("ingest")
def ingest_measured(
    paths: Annotated[list[Path], typer.Argument(help="Источники для загрузки с замером")],
    output: Annotated[Path, typer.Option("--output", "-o", help="Каталог эксперимента")],
    phase: Annotated[
        Phase, typer.Option(help="Метка замера: первичная сборка (build) или обновление (update)")
    ] = Phase.BUILD,
) -> None:
    """Загрузить документы с замером времени и токенов для отчёта о затратах.

    Каждая попытка, включая неудачные, дописывается в processing.jsonl каталога
    эксперимента. Замеры делаются до eval run: в каталог с уже готовым планом ответов
    команда не пишет. Код выхода 1, если какой-то файл или страница wiki не
    обработались."""
    settings = common.get_settings()
    try:
        with _lock(output), ExitStack() as locks:
            corpus_lock = common.corpus_lock(settings)
            locks.callback(corpus_lock.release)
            if (output / "run.json").exists():
                raise ValueError(
                    "У этого эксперимента уже есть план ответов. "
                    "Для замеров обновлений укажите новый каталог --output."
                )
            common.preflight(settings, common.INGEST_CHECKS)
            application = common.build_app(settings)
            try:
                with render.progress() as progress:
                    result = asyncio.run(
                        measured_ingest(
                            application,
                            paths,
                            settings=settings,
                            phase=phase.value,
                            output=output / "processing.jsonl",
                            progress=progress,
                            retry_failed=True,
                        )
                    )
            finally:
                application.close()
    except (OSError, ValueError, RuntimeError, filelock.Timeout, LockBusyError) as exc:
        common.fail(exc)
    common.console.print(f"Замеры обработки: {escape(str(output / 'processing.jsonl'))}")
    if result.failed or (result.wiki and result.wiki.failed):
        raise typer.Exit(1)


@app.command()
def benchmark(
    dataset: Annotated[Path, typer.Argument(help="Набор вопросов в JSONL")],
    output: Annotated[Path, typer.Option("--output", "-o", help="Каталог запуска")],
    mode: Annotated[
        list[Mode] | None,
        typer.Option(
            "--mode", "-m", help="Режим; повторите для нескольких (по умолчанию rag, wiki, graph)"
        ),
    ] = None,
    limit: Annotated[int | None, typer.Option(min=1, help="Взять только первые N вопросов")] = None,
    k: Annotated[
        int | None, typer.Option(min=1, help="Сколько результатов берёт каждый retriever")
    ] = None,
    skip_score: Annotated[
        bool, typer.Option(help="Сохранить ответы и отчёт без вызова судьи")
    ] = False,
) -> None:
    """Полный прогон: ответы, оценка Ragas и отчёты.

    Повторный запуск той же команды продолжает с места остановки. Неудавшиеся ответы не
    останавливают оценку остальных. С --skip-score судья не вызывается, строятся только
    отчёты. Код выхода 1, если прогон неполный."""
    # Неудавшиеся ответы не останавливают оценку остальных: их повторит следующий запуск,
    # а код выхода 1 сообщит о неполном прогоне.
    partial = False
    try:
        run(dataset, output, mode=mode, limit=limit, k=k)
    except typer.Exit as exc:
        if exc.exit_code != 1:
            raise
        partial = True
    if skip_score:
        report_command(output)
    else:
        score(output)
    if partial:
        raise typer.Exit(1)
