"""Typer commands, wired into the existing CLI without constructing an App on import."""

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

app = typer.Typer(no_args_is_help=True, help="Compare retrieval modes with Ragas metrics.")
common.app.add_typer(app, name="eval")


@contextmanager
def _lock(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    with filelock.FileLock(output / ".eval.lock", timeout=0):
        yield


@contextmanager
def _progress(console):
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
                f"Planned: {total}; completed: {total - remaining}; remaining: {remaining}"
            )
            task = bars.add_task("starting", total=total, completed=total - remaining)
        elif event == "item":
            bars.update(task, description=escape(fields["label"]))
        elif event == "done":
            if fields["status"] in ("error", "undefined"):
                failed += 1
                console.print(f"[red]{escape(fields['label'])}: {escape(fields['detail'])}[/red]")
            bars.update(task, advance=1)

    with bars:
        yield update
    console.print(f"Finished phase; failed/undefined attempts: {failed}")


def _print_summary(output: Path, console):
    summary = report(output)
    table = Table("approach", "metric", "mean", "ok/planned", "skipped", "error/undefined/missing")
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
    console.print(f"Reports: {escape(str(output / 'report.md'))}, details.csv, summary.csv")
    return summary


@app.command()
def run(
    dataset: Annotated[
        Path, typer.Argument(help="JSONL questions with id, question, optional reference.")
    ],
    output: Annotated[Path, typer.Option("--output", "-o", help="New or matching run directory.")],
    mode: Annotated[
        list[Mode] | None,
        typer.Option("--mode", "-m", help="Repeat to select modes (default: rag, wiki, graph)."),
    ] = None,
    limit: Annotated[int | None, typer.Option("--limit", min=1)] = None,
    k: Annotated[int | None, typer.Option("--k", min=1)] = None,
) -> None:
    """Generate and save answers in each selected mode; resume successful records."""
    try:
        load_questions(dataset)  # reject invalid input before any services/configuration
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
        f"Answers saved in {escape(str(output))}; failed: {failed}. Next: eval score {escape(str(output))}"
    )
    if failed:
        raise typer.Exit(1)


@app.command()
def score(
    output: Annotated[Path, typer.Argument(help="Saved run directory.")],
    metric: Annotated[
        list[str] | None,
        typer.Option("--metric", help="Repeat to select metrics; default: all five."),
    ] = None,
) -> None:
    """Score saved answers using a judge, without retrieval services."""
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
def report_command(output: Annotated[Path, typer.Argument(help="Saved run directory.")]) -> None:
    """Write reports from saved artifacts, without model calls or service checks."""
    try:
        load_run(output)
        with _lock(output):
            _print_summary(output, common.console)
    except (OSError, ValueError, filelock.Timeout, LockBusyError) as exc:
        common.fail(exc)


class Phase(StrEnum):
    BUILD = "build"
    UPDATE = "update"


@app.command("ingest")
def ingest_measured(
    paths: Annotated[list[Path], typer.Argument(help="Sources to ingest and measure.")],
    output: Annotated[Path, typer.Option("--output", "-o", help="Experiment directory.")],
    phase: Annotated[
        Phase, typer.Option(help="Label initial construction or later changes.")
    ] = Phase.BUILD,
) -> None:
    """Measure shared preprocessing, keeping failed attempts in processing.jsonl."""
    settings = common.get_settings()
    try:
        with _lock(output), ExitStack() as locks:
            corpus_lock = common.corpus_lock(settings)
            locks.callback(corpus_lock.release)
            if (output / "run.json").exists():
                raise ValueError(
                    "This experiment already has an answer plan; use a new output directory for updates"
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
    common.console.print(f"Processing measurements: {escape(str(output / 'processing.jsonl'))}")
    if result.failed or (result.wiki and result.wiki.failed):
        raise typer.Exit(1)


@app.command()
def benchmark(
    dataset: Annotated[Path, typer.Argument(help="Question dataset JSONL.")],
    output: Annotated[Path, typer.Option("--output", "-o")],
    mode: Annotated[list[Mode] | None, typer.Option("--mode", "-m")] = None,
    limit: Annotated[int | None, typer.Option(min=1)] = None,
    k: Annotated[int | None, typer.Option(min=1)] = None,
    skip_score: Annotated[
        bool, typer.Option(help="Save answers/report without calling a judge.")
    ] = False,
) -> None:
    """Run answers, Ragas scoring and reports; rerun the same command to resume."""
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
