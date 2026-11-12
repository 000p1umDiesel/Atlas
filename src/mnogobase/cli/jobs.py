"""Очередь фонового ingest (`jobs enqueue | list | retry`) и локальный worker (`worker run`).

Очередь — таблица `jobs` в схеме проекта PostgreSQL (`background.queue.JobQueue`). Задачи
ставят `jobs enqueue`, watcher worker'а и HTTP API (загрузка и удаление документов); сам
worker запускается только явной командой, при импорте ничего не стартует.

Как устроен `worker run`:

1. **Один worker на проект.** Сначала берётся блокировка `worker` (advisory-блокировка
   PostgreSQL); второй worker получает отказ с кодом 2. Под ней `JobQueue.recover`
   возвращает в очередь задачи, оставшиеся `running` после аварийной остановки, а задачи,
   исчерпавшие попытки, помечает failed (`InterruptedTooOften`), чтобы файл, который роняет
   процесс, не брался бесконечно.
2. **Watcher.** С `--watch` в отдельном потоке-демоне `_watch_loop` раз в `--interval`
   секунд опрашивает каталоги и ставит в очередь новые и изменённые файлы, дождавшись, пока
   файл перестанет меняться (`--settle`). Поток работает со своим соединением к очереди.
   Если он упал, основной цикл видит это через список `health` и останавливает worker:
   молча перестать следить за каталогами хуже, чем упасть.
3. **Цикл `dispatch`.** На каждом шаге worker отмечает heartbeat (его показывает
   `/api/status`) и вызывает `process_one`: тот берёт блокировку корпуса (`ingest`) и
   только под ней забирает одну готовую задачу (`claim`). Поэтому worker и CLI-писатели
   (ingest, remove, wiki build, reindex, reset) никогда не пишут корпус одновременно: если
   блокировка занята, worker просто ждёт `--interval` и пробует снова. Время одной задачи
   ограничено `jobs.timeout_s`.
4. **Задача.** `handle` выполняет ingest или remove. Перед ingest сверяется хеш файла:
   если файл изменился после постановки в очередь, ставится новая задача на новую версию
   (с задержкой, чтобы файл успел «осесть»), а текущая помечается superseded. Ход работы
   (`JobProgress`) пишется в строку задачи и одновременно рисуется в терминале.
5. **Ошибки.** Ошибка документа расходует попытку, задача повторяется с удваивающейся
   задержкой и после `max_attempts` становится failed. Недоступный сервис моделей или базы
   (`ServicesUnavailable`, ошибки PostgreSQL) — не вина документа: задача откладывается без
   расхода попытки (`release`). После ошибки базы соединения `App` могли умереть (рестарт
   PostgreSQL), поэтому `App` помечается устаревшим и пересоздаётся перед следующей задачей.
   Перед каждой задачей сервисы проверяются заново (`services`), и `App` создаётся лениво.
6. **Остановка.** SIGTERM (`docker stop`, systemd) переназначен на обработчик SIGINT, так что
   и он, и Ctrl+C превращаются в `KeyboardInterrupt` и штатно завершают worker. Прерванная
   задача остаётся `running` и продолжается при следующем запуске (через `recover`). В
   `finally` останавливается watcher, закрываются `App` и очередь, восстанавливается прежний
   обработчик SIGTERM и отпускается блокировка worker.
"""

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

jobs_app = typer.Typer(no_args_is_help=True, help="Постоянная локальная очередь загрузки")
worker_app = typer.Typer(
    no_args_is_help=True, help="Локальный worker фоновой загрузки (один на проект)"
)
common.app.add_typer(jobs_app, name="jobs")
common.app.add_typer(worker_app, name="worker")


def _excluded(settings):
    """Каталоги, которые нельзя ставить в очередь: данные, wiki, логи и прогоны самого
    проекта. Иначе watcher, следящий за корнем проекта, загружал бы сгенерированную wiki."""
    return [settings.data_dir, settings.wiki.dir, settings.logs_dir, settings.runs_dir]


@jobs_app.command()
def enqueue(
    paths: Annotated[list[Path], typer.Argument(help="Файлы или каталоги")],
    force: Annotated[
        bool, typer.Option(help="Поставить заново уже выполненное или упавшее содержимое")
    ] = False,
) -> None:
    """Поставить файлы в очередь фонового ingest.

    Каталоги обходятся рекурсивно; берутся только расширения из parsing.extensions,
    скрытые файлы и служебные каталоги проекта пропускаются. Уже выполненная версия файла
    без --force повторно не ставится. Очередь обрабатывает mnogobase worker run. Код
    выхода 2, если подходящих файлов не нашлось."""
    settings = common.get_settings()
    files = discover(paths, settings.parsing.extensions, _excluded(settings))
    if not files:
        common.fail(
            ValueError(
                "Подходящих файлов не найдено. Проверьте пути и расширения в parsing.extensions."
            )
        )
    q = JobQueue(database_target(settings))
    try:
        for path in files:
            job_id = q.enqueue(path, file_doc_id(path), force=force)
            common.console.print(f"задача {job_id}: {escape(str(path))}")
    except OSError as exc:
        common.fail(exc)
    finally:
        q.close()


@jobs_app.command("list")
def list_jobs(
    limit: Annotated[int, typer.Option(min=1, help="Сколько последних задач показать")] = 100,
) -> None:
    """Показать задачи очереди: статус, попытки, прогресс и ошибки.

    Сначала печатаются счётчики по статусам, затем последние задачи (не больше --limit) и
    их полные пути. Для отложенной задачи с ошибкой видно время следующей попытки."""
    settings = common.get_settings()
    target = database_target(settings)
    if not target_exists(target, "jobs"):
        common.console.print("Очередь пуста.")
        return
    q = JobQueue(target)
    try:
        common.console.print(str(q.counts()))
        table = Table("id", "статус", "попытки", "путь", "прогресс", "ошибка", "повтор в (Unix)")
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
def retry(
    job_id: Annotated[
        int | None, typer.Option("--id", min=1, help="Вернуть только эту задачу")
    ] = None,
) -> None:
    """Вернуть упавшие задачи в очередь: все или одну по --id.

    Счётчик попыток и ошибка сбрасываются; печатается число возвращённых задач. Выполнит
    их запущенный worker."""
    settings = common.get_settings()
    target = database_target(settings)
    if not target_exists(target, "jobs"):
        common.console.print("Очередь пуста.")
        return
    q = JobQueue(target)
    try:
        common.console.print(f"Возвращено в очередь: {q.retry(job_id)}")
    finally:
        q.close()


class JobProgress(NullProgress):
    """`ProgressSink` задачи очереди: передаёт события пайплайна в терминальный прогресс
    (`delegate`) и сохраняет сводку текущего шага в строке задачи.

    Сводка вида `embed 12/40; ошибок 0` видна в `jobs list` и в API. Каждое сохранение
    заодно обновляет heartbeat worker'а: долгая задача не выглядит как зависший worker."""

    def __init__(self, queue, job_id, delegate):
        self.queue, self.job_id, self.delegate = queue, job_id, delegate
        self.stage, self.total, self.completed, self.failed = "подготовка", None, 0, 0

    def files_found(self, total):
        """Передаёт число файлов в терминальный прогресс."""
        self.delegate.files_found(total)

    def file_started(self, path):
        """Передаёт начало обработки файла в терминальный прогресс."""
        self.delegate.file_started(path)

    def file_done(self, status):
        """Передаёт итог файла в терминальный прогресс."""
        self.delegate.file_done(status)

    def step(self, name, total=None):
        """Начинает новый шаг: счётчики обнуляются, сводка сохраняется в задачу."""
        self.stage, self.total, self.completed, self.failed = name, total, 0, 0
        self.delegate.step(name, total)
        self._save()

    def advance(self, n=1, *, failed=False):
        """Учитывает `n` выполненных (или упавших) единиц шага и сохраняет сводку."""
        self.completed += n
        self.failed += n if failed else 0
        self.delegate.advance(n, failed=failed)
        self._save()

    def _save(self):
        """Пишет сводку шага в строку задачи и обновляет heartbeat (два UPDATE в
        PostgreSQL на каждое событие)."""
        count = f" {self.completed}/{self.total}" if self.total is not None else ""
        self.queue.set_progress(self.job_id, f"{self.stage}{count}; ошибок {self.failed}")
        self.queue.heartbeat()


def _watch_loop(db, settings, roots, stop, interval, settle, health):
    """Тело потока watcher: опрашивает каталоги `roots`, пока не выставлен `stop`.

    Использует собственное соединение с очередью (`db` — цель базы проекта): соединения не
    делятся между потоками. Опрос раз в `interval` секунд вместо событий файловой системы
    переживает пропущенные события и не зависит от платформы. Любая ошибка записывается
    в `health` (только имя типа, без текста) и завершает поток; основной цикл worker'а
    увидит её и остановится."""
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
        list[Path] | None, typer.Option("--watch", help="Папка для слежения; повторите для каждой")
    ] = None,
    once: Annotated[
        bool, typer.Option(help="Выполнить готовые задачи и выйти, без слежения")
    ] = False,
    interval: Annotated[
        float, typer.Option(min=0.1, help="Пауза между опросами очереди и папок, секунд")
    ] = 2.0,
    settle: Annotated[
        float, typer.Option(min=0, help="Сколько секунд файл должен не меняться перед постановкой")
    ] = 2.0,
    max_attempts: Annotated[
        int | None, typer.Option(min=1, help="По умолчанию jobs.max_attempts")
    ] = None,
    retry_delay: Annotated[
        float | None, typer.Option(min=0.1, help="По умолчанию jobs.retry_delay_s")
    ] = None,
) -> None:
    """Запустить фоновый worker, который выполняет задачи очереди.

    Worker — единственный писатель корпуса: задачи ingest и remove выполняются по одной
    под блокировкой корпуса, поэтому CLI-команды записи и worker не мешают друг другу. С
    --watch он ещё и следит за каталогами, ставя в очередь новые и изменённые файлы. Если
    недоступен сервер моделей или база, задача откладывается без расхода попытки. Ctrl+C
    или SIGTERM останавливают worker, прерванная задача продолжится при следующем запуске.
    С --once worker выполняет готовые задачи и завершается с кодом 1, если в очереди
    остались незавершённые или упавшие. Код выхода 2, если worker уже запущен или упал
    watcher."""
    settings = common.get_settings()
    if once and watch:
        common.fail(
            ValueError(
                "--once и --watch несовместимы: --watch работает непрерывно. "
                "Поставьте файлы командой `mnogobase jobs enqueue`, затем запустите `worker run --once`."
            )
        )
    if any(not p.is_dir() for p in watch or []):
        common.fail(ValueError("Каждый путь --watch должен быть существующим каталогом."))
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    worker_lock = database_lock(settings, "worker")
    try:
        worker_lock.acquire()
    except LockBusyError:
        common.fail(
            ValueError(
                "Для этого проекта уже запущен другой worker. Остановите его или дождитесь завершения."
            )
        )
    q, application = None, None
    previous_term = None
    stop, health, thread = threading.Event(), [], None
    try:
        if threading.current_thread() is threading.main_thread():
            # SIGTERM ведёт себя как Ctrl+C: KeyboardInterrupt штатно завершает цикл ниже.
            # signal.signal работает только в главном потоке; вне его SIGTERM не трогаем.
            previous_term = signal.signal(signal.SIGTERM, signal.default_int_handler)
        max_attempts = max_attempts or settings.jobs.max_attempts
        retry_delay = retry_delay or settings.jobs.retry_delay_s
        q = JobQueue(database_target(settings))
        resumed = q.recover(max_attempts)
        common.console.print(
            f"Worker запущен; восстановлено задач: {resumed}; очередь: {q.counts()}"
        )
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
        # True после ошибки базы: соединения App могли умереть, его надо создать заново
        stale = False

        async def services():
            """Проверяет сервисы и возвращает рабочий `App`, создавая его при необходимости.

            Проверка идёт перед каждой задачей: если что-то недоступно,
            `ServicesUnavailable` откладывает задачу без расхода попытки. Устаревший после
            ошибки базы `App` закрывается и строится заново."""
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
            """Выполняет задачу remove: забывает документ по пути задачи и пересобирает wiki.

            Файл, загруженный через API (лежит в `service.uploads_dir`), после этого
            удаляется вместе с записью о загрузке; любые другие файлы остаются на диске.
            Неудачная пересборка wiki считается ошибкой задачи (будет повтор)."""
            doc_id = json.loads(job.payload_json or "{}").get("doc_id")
            path = Path(job.path)
            app = await services()
            common.console.print(f"Удаление, задача {job.id}: {escape(job.path)}")
            with render.progress() as progress:
                report = await app.pipeline.remove(
                    [path], run_id=new_run_id(), progress=JobProgress(q, job.id, progress)
                )
            if report.wiki and report.wiki.failed:
                raise RuntimeError(
                    "Не удалось пересобрать wiki. Подробности — в `mnogobase status` и логах."
                )
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
            """Обработчик одной задачи для `process_one`: remove или ingest.

            Для ingest файл должен существовать и совпадать по хешу с версией в задаче.
            Изменившийся файл не обрабатывается: на новую версию ставится новая задача,
            отложенная на время «оседания» файла, а текущая помечается superseded.
            `verify_sources` и `expected` заставляют ingest ещё раз сверить файл во время
            обработки. Неполный результат (упал файл или страница wiki) — исключение,
            чтобы `process_one` засчитал попытку. `job_scope` помечает вызовы LLM номером
            задачи для учёта расхода."""
            q.heartbeat()
            if job.kind == "remove":
                with job_scope(job.id):
                    await remove(job)
                return
            if job.kind != "ingest":
                raise ValueError("Неподдерживаемый тип задачи")
            if not Path(job.path).is_file():
                raise FileNotFoundError(job.path)
            current = file_doc_id(Path(job.path))
            if current != job.fingerprint:
                replacement = q.enqueue(Path(job.path), current)
                q.defer(replacement, max(settle, interval))
                q.supersede(job.id)
                return
            app = await services()
            common.console.print(f"Обработка, задача {job.id}: {escape(job.path)}")
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
                raise RuntimeError(
                    "Загрузка или wiki завершились с ошибкой. Подробности — в `mnogobase status` и логах."
                )

        def reconnect(exc):
            """Колбэк `process_one` на временную ошибку: задача уже отложена, здесь только
            сообщение и, для ошибки базы, отметка о пересоздании `App`.

            Соединения App могли оборваться (рестарт PostgreSQL): следующая задача
            создаст App заново."""
            nonlocal stale
            common.console.print(f"Сервисы недоступны ({type(exc).__name__}); задача отложена.")
            if isinstance(exc, DatabaseError | DBAPIError):
                stale = True

        async def dispatch():
            """Основной цикл: heartbeat, проверка watcher'а, одна задача за итерацию.

            Если задачи не нашлось (очередь пуста или корпус занят CLI-писателем), worker
            спит `interval` секунд, а с `--once` выходит из цикла."""
            while True:
                q.heartbeat()
                if health:
                    raise RuntimeError(f"Watcher остановился: {health[0]}. Перезапустите worker.")
                processed = await process_one(
                    q,
                    handle,
                    max_attempts=max_attempts,
                    delay=retry_delay,
                    timeout=settings.jobs.timeout_s,
                    on_transient=reconnect,
                )
                if processed:
                    common.console.print(f"Очередь: {q.counts()}")
                elif once:
                    break
                else:
                    await asyncio.sleep(interval)

        try:
            asyncio.run(dispatch())
        except (KeyboardInterrupt, asyncio.CancelledError):
            common.console.print(
                "Worker остановлен; незавершённая задача продолжится при следующем запуске."
            )
        except (OSError, ValueError, RuntimeError) as exc:
            common.fail(exc)
        counts = q.counts()
        common.console.print(f"Очередь: {counts}")
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
