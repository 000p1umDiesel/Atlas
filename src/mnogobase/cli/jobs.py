"""Local worker and queue commands; starting a worker is an explicit user action."""

from __future__ import annotations

import asyncio
import json
import signal
import threading
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table
from sqlalchemy.exc import DBAPIError

from mnogobase.background.queue import JobQueue
from mnogobase.background.uploads import Uploads
from mnogobase.background.watch import Watcher, discover
from mnogobase.background.worker import ServicesUnavailable, process_one
from mnogobase.cli import common, render
from mnogobase.core.database import (
    DatabaseError,
    LockBusyError,
    database_lock,
    database_target,
    target_exists,
)
from mnogobase.core.ids import file_doc_id
from mnogobase.core.log import get_logger, new_run_id
from mnogobase.core.progress import NullProgress
from mnogobase.ingest.processing import measured_ingest
from mnogobase.llm.telemetry import job_scope

jobs_app = typer.Typer(no_args_is_help=True, help="Persistent local ingest queue.")
worker_app = typer.Typer(no_args_is_help=True, help="Run a single local ingest worker.")
common.app.add_typer(jobs_app, name="jobs")
common.app.add_typer(worker_app, name="worker")


def _excluded(settings):
    return [settings.data_dir, settings.wiki.dir, settings.logs_dir, settings.runs_dir]


@jobs_app.command()
def enqueue(
    paths: Annotated[list[Path], typer.Argument(help="Files or directories.")],
    force: Annotated[
        bool, typer.Option(help="Requeue previously completed/failed content.")
    ] = False,
) -> None:
    settings = common.get_settings()
    files = discover(paths, settings.parsing.extensions, _excluded(settings))
    if not files:
        common.fail(ValueError("No supported source files found"))
    q = JobQueue(database_target(settings))
    try:
        for path in files:
            job_id = q.enqueue(path, file_doc_id(path), force=force)
            common.console.print(f"job {job_id}: {escape(str(path))}")
    except OSError as exc:
        common.fail(exc)
    finally:
        q.close()


@jobs_app.command("list")
def list_jobs(limit: Annotated[int, typer.Option(min=1)] = 100) -> None:
    settings = common.get_settings()
    target = database_target(settings)
    if not target_exists(target, "jobs"):
        common.console.print("Queue is empty.")
        return
    q = JobQueue(target)
    try:
        common.console.print(str(q.counts()))
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
        common.console.print(table)
        for job in q.jobs(limit):
            common.console.print(f"{job.id}: {escape(job.path)}", soft_wrap=True)
    finally:
        q.close()


@jobs_app.command()
def retry(job_id: Annotated[int | None, typer.Option("--id", min=1)] = None) -> None:
    settings = common.get_settings()
    target = database_target(settings)
    if not target_exists(target, "jobs"):
        common.console.print("Queue is empty.")
        return
    q = JobQueue(target)
    try:
        common.console.print(f"Requeued: {q.retry(job_id)}")
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
        self.queue.heartbeat()


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
    max_attempts: Annotated[
        int | None, typer.Option(min=1, help="Default: jobs.max_attempts.")
    ] = None,
    retry_delay: Annotated[
        float | None, typer.Option(min=0.1, help="Default: jobs.retry_delay_s.")
    ] = None,
) -> None:
    settings = common.get_settings()
    if once and watch:
        common.fail(ValueError("Use jobs enqueue before worker run --once; --watch is continuous"))
    if any(not p.is_dir() for p in watch or []):
        common.fail(ValueError("Every --watch path must be an existing directory"))
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    worker_lock = database_lock(settings, "worker")
    try:
        worker_lock.acquire()
    except LockBusyError:
        common.fail(ValueError("Another worker is already running for this project"))
    q, application = None, None
    previous_term = None
    stop, health, thread = threading.Event(), [], None
    try:
        if threading.current_thread() is threading.main_thread():
            previous_term = signal.signal(signal.SIGTERM, signal.default_int_handler)
        max_attempts = max_attempts or settings.jobs.max_attempts
        retry_delay = retry_delay or settings.jobs.retry_delay_s
        q = JobQueue(database_target(settings))
        resumed = q.recover(max_attempts)
        common.console.print(f"Worker started; recovered {resumed}; queue {q.counts()}")
        if watch:
            thread = threading.Thread(
                target=_watch_loop,
                args=(
                    database_target(settings),
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

        uploads = Uploads(settings)
        stale = False

        async def services():
            nonlocal application, stale
            failed = [c for c in common.run_checks(settings, only=common.INGEST_CHECKS) if not c.ok]
            if failed:
                raise ServicesUnavailable(", ".join(c.name for c in failed))
            if application is not None and stale:
                await application.aclose()
                application = None
            stale = False
            if application is None:
                application = common.build_app(settings)
            return application

        async def remove(job):
            # Забыть документ; загруженный через API файл удаляется, остальные остаются.
            doc_id = json.loads(job.payload_json or "{}").get("doc_id")
            path = Path(job.path)
            app = await services()
            common.console.print(f"Removing job {job.id}: {escape(job.path)}")
            with render.progress() as progress:
                report = await app.pipeline.remove(
                    [path], run_id=new_run_id(), progress=JobProgress(q, job.id, progress)
                )
            if report.wiki and report.wiki.failed:
                raise RuntimeError("Wiki rebuild failed; see project status/logs")
            try:
                uploaded = uploads.contained(path, must_exist=False)
            except FileNotFoundError:
                return
            # Пока шло удаление, файл могли загрузить снова (desired уже не remove:...): тогда
            # он нужен новой задаче ingest. Проверка и удаление — под блокировкой пути, которую
            # берёт и загрузка через API.
            with q.locked_path(job.path):
                if not q.is_current(job):
                    return
                uploaded.unlink(missing_ok=True)
                if doc_id:
                    uploads.delete(doc_id)

        async def handle(job):
            q.heartbeat()
            if job.kind == "remove":
                with job_scope(job.id):
                    await remove(job)
                return
            if job.kind != "ingest":
                raise ValueError("Unsupported job kind")
            if not Path(job.path).is_file():
                raise FileNotFoundError(job.path)
            current = file_doc_id(Path(job.path))
            if current != job.fingerprint:
                replacement = q.enqueue(Path(job.path), current)
                q.defer(replacement, max(settle, interval))
                q.supersede(job.id)
                return
            app = await services()
            common.console.print(f"Processing job {job.id}: {escape(job.path)}")
            with render.progress() as progress, job_scope(job.id):
                report = await measured_ingest(
                    app,
                    [Path(job.path)],
                    settings=settings,
                    verify_sources=True,
                    expected={job.path: job.fingerprint},
                    snapshot=False,
                    retry_failed=True,
                    run_id=new_run_id(),
                    progress=JobProgress(q, job.id, progress),
                )
            if report.failed or (report.wiki and report.wiki.failed):
                raise RuntimeError("Ingest or wiki failed; see project status/logs")

        def reconnect(exc):
            # Соединения App могли оборваться (рестарт PostgreSQL): следующая задача
            # создаст App заново.
            nonlocal stale
            common.console.print(f"Services unavailable ({type(exc).__name__}); job deferred.")
            if isinstance(exc, DatabaseError | DBAPIError):
                stale = True

        async def dispatch():
            while True:
                q.heartbeat()
                if health:
                    raise RuntimeError(f"Watcher stopped: {health[0]}; restart the worker")
                processed = await process_one(
                    q,
                    handle,
                    max_attempts=max_attempts,
                    delay=retry_delay,
                    timeout=settings.jobs.timeout_s,
                    on_transient=reconnect,
                )
                if processed:
                    common.console.print(f"Queue: {q.counts()}")
                elif once:
                    break
                else:
                    await asyncio.sleep(interval)

        try:
            asyncio.run(dispatch())
        except (KeyboardInterrupt, asyncio.CancelledError):
            common.console.print("Worker stopped; unfinished job will resume on restart.")
        except (OSError, ValueError, RuntimeError) as exc:
            common.fail(exc)
        counts = q.counts()
        common.console.print(f"Queue: {counts}")
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
