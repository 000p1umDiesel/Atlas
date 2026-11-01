"""Typer commands, wired into the existing CLI without constructing an App on import."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import filelock
import typer
from rich.markup import escape
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from mnogobase.retrieval import Mode

from .core import load_questions, load_run, prepare_run, report, run_answers, score_run
from .ragas import judge_config

app = typer.Typer(no_args_is_help=True, help="Compare retrieval modes with Ragas metrics.")


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
    from mnogobase import cli

    try:
        load_questions(dataset)  # reject invalid input before any services/configuration
        settings = cli._settings(project=True)
        with _lock(output):
            prepare_run(
                dataset,
                output,
                settings,
                modes=[m.value for m in mode] if mode else None,
                limit=limit,
                k=k,
            )
            cli._preflight(settings)
            application = cli.build_app(settings)
            try:
                retrievers, answerer, translator = cli._answer_setup(application)
                with _progress(cli.console) as progress:
                    failed = asyncio.run(
                        run_answers(output, retrievers, answerer, translator, progress=progress)
                    )
            finally:
                application.close()
            report(output)
    except (OSError, ValueError, filelock.Timeout) as exc:
        cli._fail_cleanly(exc)
    cli.console.print(
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
    from mnogobase import cli

    try:
        load_run(output)
        settings = cli._settings()
        with _lock(output):
            with _progress(cli.console) as progress:
                failed = asyncio.run(
                    score_run(output, judge_config(settings), names=metric, progress=progress)
                )
            summary = _print_summary(output, cli.console)
    except (OSError, ValueError, filelock.Timeout) as exc:
        cli._fail_cleanly(exc)
    if failed or any(s["answers_error"] or s["answers_missing"] for s in summary):
        raise typer.Exit(1)


@app.command(name="report")
def report_command(output: Annotated[Path, typer.Argument(help="Saved run directory.")]) -> None:
    """Write reports from saved artifacts, without model calls or service checks."""
    from mnogobase import cli

    try:
        load_run(output)
        with _lock(output):
            _print_summary(output, cli.console)
    except (OSError, ValueError, filelock.Timeout) as exc:
        cli._fail_cleanly(exc)
