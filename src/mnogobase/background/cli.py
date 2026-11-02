"""Local worker and queue commands; starting a worker is an explicit user action."""

from __future__ import annotations

import asyncio
import signal
import threading
from pathlib import Path
from typing import Annotated

import typer
from filelock import FileLock, Timeout
from rich.markup import escape
from rich.table import Table

from mnogobase.ids import file_doc_id
from mnogobase.log import get_logger, new_run_id
from mnogobase.processing import measured_ingest
from mnogobase.progress import NullProgress

from .queue import JobQueue
from .watch import Watcher, discover
from .worker import process_one

jobs_app = typer.Typer(no_args_is_help=True, help="Persistent local ingest queue.")
worker_app = typer.Typer(no_args_is_help=True, help="Run a single local ingest worker.")


def _excluded(settings):
    return [settings.data_dir, settings.wiki.dir, settings.logs_dir, settings.runs_dir]


@jobs_app.command()
def enqueue(
    paths: Annotated[list[Path], typer.Argument(help="Files or directories.")],
    force: Annotated[
        bool, typer.Option(help="Requeue previously completed/failed content.")
    ] = False,
) -> None:
    from mnogobase import cli

    settings = cli._settings()
    files = discover(paths, settings.parsing.extensions, _excluded(settings))
    if not files:
        cli._fail_cleanly(ValueError("No supported source files found"))
    q = JobQueue(settings.data_dir / "jobs.db")
    try:
        for path in files:
            job_id = q.enqueue(path, file_doc_id(path), force=force)
            cli.console.print(f"job {job_id}: {escape(str(path))}")
    except OSError as exc:
        cli._fail_cleanly(exc)
    finally:
        q.close()


@jobs_app.command("list")
def list_jobs(limit: Annotated[int, typer.Option(min=1)] = 100) -> None:
    from mnogobase import cli

    settings = cli._settings()
    path = settings.data_dir / "jobs.db"
    if not path.exists():
        cli.console.print("Queue is empty.")
        return
    q = JobQueue(path)
    try:
        cli.console.print(str(q.counts()))
        table = Table("id", "status", "attempts", "path", "progress", "error", "retry at (Unix)")
        for job in q.jobs(limit):
            table.add_row(
                str(job.id),
                job.status,
                str(job.attempts),
                escape(Path(job.path).name),
                escape(job.progress),
                escape(job.error or ""),
                str(int(job.available_at)) if job.status == "pending" and job.error else "",
            )
        cli.console.print(table)
        for job in q.jobs(limit):
            cli.console.print(f"{job.id}: {escape(job.path)}", soft_wrap=True)
    finally:
        q.close()


@jobs_app.command()
def retry(job_id: Annotated[int | None, typer.Option("--id", min=1)] = None) -> None:
    from mnogobase import cli

    settings = cli._settings()
    path = settings.data_dir / "jobs.db"
    if not path.exists():
        cli.console.print("Queue is empty.")
        return
    q = JobQueue(path)
    try:
        cli.console.print(f"Requeued: {q.retry(job_id)}")
    finally:
        q.close()


class JobProgress(NullProgress):
    def __init__(self, queue, job_id, delegate):
        self.queue, self.job_id, self.delegate = queue, job_id, delegate
        self.stage, self.total, self.completed, self.failed = "starting", None, 0, 0

    def files_found(self, total):
        self.delegate.files_found(total)

    def file_started(self, path):
        self.delegate.file_started(path)

    def file_done(self, status):
        self.delegate.file_done(status)

    def step(self, name, total=None):
        self.stage, self.total, self.completed, self.failed = name, total, 0, 0
        self.delegate.step(name, total)
        self._save()

    def advance(self, n=1, *, failed=False):
        self.completed += n
        self.failed += n if failed else 0
        self.delegate.advance(n, failed=failed)
        self._save()

    def _save(self):
        count = f" {self.completed}/{self.total}" if self.total is not None else ""
        self.queue.set_progress(self.job_id, f"{self.stage}{count}; failed {self.failed}")


def _watch_loop(db, settings, roots, stop, interval, settle, health):
    q = None
    try:
        q = JobQueue(db)
        watcher = Watcher(
            q, roots, settings.parsing.extensions, settle=settle, excluded=_excluded(settings)
        )
        while not stop.is_set():
            watcher.scan()
            stop.wait(interval)
    except Exception as exc:
        health.append(type(exc).__name__)
        get_logger(__name__).error("watcher_failed", error_type=type(exc).__name__)
    finally:
        if q is not None:
            q.close()


@worker_app.command("run")
def run_worker(
    watch: Annotated[
        list[Path] | None, typer.Option("--watch", help="Repeat for each folder.")
    ] = None,
    once: Annotated[bool, typer.Option(help="Drain ready jobs, then exit; no watching.")] = False,
    interval: Annotated[float, typer.Option(min=0.1)] = 2.0,
    settle: Annotated[float, typer.Option(min=0)] = 2.0,
    max_attempts: Annotated[int, typer.Option(min=1)] = 3,
    retry_delay: Annotated[float, typer.Option(min=0.1)] = 10.0,
) -> None:
    from mnogobase import cli

    settings = cli._settings()
    if settings.neo4j.team_id is not None:
        cli._fail_cleanly(
            ValueError("Team queues require the typed dispatcher: use `mnogobase service work`")
        )
    if once and watch:
        cli._fail_cleanly(
            ValueError("Use jobs enqueue before worker run --once; --watch is continuous")
        )
    if any(not p.is_dir() for p in watch or []):
        cli._fail_cleanly(ValueError("Every --watch path must be an existing directory"))
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    worker_lock = FileLock(settings.data_dir / "worker.lock", timeout=0)
    try:
        worker_lock.acquire()
    except Timeout:
        cli._fail_cleanly(ValueError("Another worker is already running for this project"))
    q, application = None, None
    previous_term = None
    stop, health, thread = threading.Event(), [], None
    try:
        if threading.current_thread() is threading.main_thread():
            previous_term = signal.signal(signal.SIGTERM, signal.default_int_handler)
        q = JobQueue(settings.data_dir / "jobs.db")
        resumed = q.recover()
        cli.console.print(f"Worker started; recovered {resumed}; queue {q.counts()}")
        if watch:
            thread = threading.Thread(
                target=_watch_loop,
                args=(
                    settings.data_dir / "jobs.db",
                    settings,
                    watch,
                    stop,
                    interval,
                    settle,
                    health,
                ),
                daemon=True,
                name="mnogobase-watch",
            )
            thread.start()

        async def handle(job):
            nonlocal application
            if not Path(job.path).is_file():
                raise FileNotFoundError(job.path)
            current = file_doc_id(Path(job.path))
            if current != job.fingerprint:
                replacement = q.enqueue(Path(job.path), current)
                q.defer(replacement, max(settle, interval))
                q.supersede(job.id)
                return
            cli._preflight(settings, cli.INGEST_CHECKS)
            if application is None:
                application = cli.build_app(settings)
            cli.console.print(f"Processing job {job.id}: {escape(job.path)}")
            with cli._progress() as progress:
                report = await measured_ingest(
                    application,
                    [Path(job.path)],
                    settings=settings,
                    verify_sources=True,
                    expected={job.path: job.fingerprint},
                    retry_failed=True,
                    run_id=new_run_id(),
                    progress=JobProgress(q, job.id, progress),
                )
            if report.failed or (report.wiki and report.wiki.failed):
                raise RuntimeError("Ingest or wiki failed; see project status/logs")

        async def dispatch():
            while True:
                if health:
                    raise RuntimeError(f"Watcher stopped: {health[0]}; restart the worker")
                processed = await process_one(
                    q, handle, settings.data_dir, max_attempts=max_attempts, delay=retry_delay
                )
                if processed:
                    cli.console.print(f"Queue: {q.counts()}")
                elif once:
                    break
                else:
                    await asyncio.sleep(interval)

        try:
            asyncio.run(dispatch())
        except (KeyboardInterrupt, asyncio.CancelledError):
            cli.console.print("Worker stopped; unfinished job will resume on restart.")
        except (OSError, ValueError, RuntimeError) as exc:
            cli._fail_cleanly(exc)
        counts = q.counts()
        cli.console.print(f"Queue: {counts}")
        if once and (
            counts.get("failed", 0) or counts.get("pending", 0) or counts.get("running", 0)
        ):
            raise typer.Exit(1)
    finally:
        stop.set()
        if thread is not None:
            thread.join()
        if application is not None:
            application.close()
        if q is not None:
            q.close()
        if previous_term is not None:
            signal.signal(signal.SIGTERM, previous_term)
        worker_lock.release()
