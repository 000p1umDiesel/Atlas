"""Надёжная очередь фоновых задач в PostgreSQL (схема проекта).

Место в потоке данных: всё, что меняет корпус в фоне, проходит через эту очередь. CLI
(`jobs enqueue`), watcher (`worker run --watch`) и HTTP API (загрузка и удаление документов)
только ставят задачи; выполняет их единственный писатель — `worker run`
(`background/worker.py:process_one` под `ingest_lock`). Очередь переживает рестарт процесса
и базы: состояние целиком в таблицах, в памяти ничего не держится.

Таблицы:
- `jobs` — одна строка на пару `(path, fingerprint)`: операция `kind` (`ingest` или
  `remove`) над абсолютным путём источника в конкретной версии. `fingerprint` у `ingest` —
  `doc_id` файла (`sha256(байты)[:16]`), у `remove` — `remove:<doc_id>`. Повторная постановка
  той же пары не создаёт новую строку, а переиспользует старую. `payload_json` — данные
  операции (для `remove` — `{"doc_id": ...}`), `progress` — короткий текст для статуса,
  `available_at` — раньше этого момента задачу не берут (задержка повтора).
- `desired` — для каждого пути последняя запрошенная версия (`fingerprint`). Задача
  «актуальна» (`is_current`), только пока её версия совпадает с `desired`; так очередь знает,
  что более старая задача того же пути уже никому не нужна.
- `worker_heartbeat` — одна строка `worker` с моментом последней активности worker'а
  (для `/api/status`).

Статусы задачи и переходы:
- `pending` → `running`: `claim` атомарно берёт самую старую готовую задачу
  (`FOR UPDATE SKIP LOCKED`) и сразу увеличивает `attempts`, поэтому попыткой считается и
  запуск, во время которого процесс упал.
- `running` → `done`: `finish`. Если завершённая задача актуальна, остальные задачи того же
  пути (`pending` / `running` / `failed`) становятся `superseded`: их работа уже не нужна.
- `running` → `pending` или `failed`: `fail` — ошибка документа. Следующая попытка через
  `delay * 2^(attempts-1)` секунд (не больше часа); после `max_attempts` попыток — `failed`.
- `running` → `pending`: `release` — сбой инфраструктуры (база, сервер моделей), попытка
  не засчитывается, задержка постоянная.
- `running` → `superseded`: `supersede` — worker увидел, что файл успел измениться, и
  поставил новую версию.
- `done` / `failed` / `superseded` → `pending`: `enqueue` с `force` или с новой версией
  пути, а также `retry` (только `failed`).
- `running` после краха процесса: `recover` при старте worker'а возвращает такие задачи в
  `pending`, а исчерпавшие попытки помечает `failed` (`InterruptedTooOften`).

Конкурентность. Операции, которые смотрят на `desired` и меняют задачи пути, выполняются в
транзакции под advisory-блокировкой `queue-path:<путь>` (`transaction_lock`, снимается
вместе с транзакцией). Её же берут API при укладке загруженного файла (`on_locked`) и worker
перед удалением файла загрузки (`locked_path`), поэтому «загрузить заново» и «удалить» один
путь не перемешиваются.

Соглашение `remove:<версия>`. API ставит удаление как задачу `kind='remove'` с
`fingerprint = "remove:<doc_id>"`, и `desired` пути становится `remove:<doc_id>`. Пока это
так, `enqueue` той же версии без `force` не создаёт задачу `ingest`, а возвращает id задачи
удаления: watcher после рестарта не загрузит обратно источник, который владелец забыл.
Загрузка через API ставит с `force=True` и потому может вернуть документ.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from mnogobase.core.database import DatabaseTarget, connect, initialize, transaction_lock

# Задача анализа графа (`kind='analyze'`) не связана с файлом: постоянный путь и версия
# дают одну задачу на проект — повторная постановка возвращает ту же (pending/running) или
# перезапускает выполненную.
ANALYZE_PATH = Path("/mnogobase/graph-analysis")
ANALYZE_VERSION = "analyze"


@dataclass(frozen=True)
class Job:
    """Снимок строки `jobs`.

    `path` — абсолютный путь источника, `fingerprint` — версия (`doc_id` или
    `remove:<doc_id>`), `status` — `pending` / `running` / `done` / `failed` / `superseded` /
    `cancelled`, `hidden` — скрыта из списка (`hide_finished`),
    `attempts` — сколько раз задачу брали в работу, `available_at` — unix-время, раньше
    которого её не берут, `error` — имя типа исключения (текст может содержать секреты);
    только у исключений из `worker.SAFE_MESSAGE_ERRORS` — ещё и их текст, `progress` — короткое описание текущего шага (для CLI), `stage` / `step_done` / `step_total` — тот же
    шаг для сайта: ключ стадии (`start`, `parse`, `embed`, `wiki draft`, `analyze`...) и счётчики
    (None — неизвестно). Метки времени — unix-секунды."""

    id: int
    path: str
    fingerprint: str
    status: str
    attempts: int
    available_at: float
    error: str | None
    progress: str
    kind: str = "ingest"
    payload_json: str | None = None
    created_at: float = 0
    updated_at: float = 0
    started_at: float | None = None
    stage: str | None = None
    step_done: int | None = None
    step_total: int | None = None
    hidden: bool = False


_JOB_FIELDS = tuple(Job.__dataclass_fields__)


def _job(row) -> Job:
    """Job по именам колонок, а не по их порядку в `SELECT *` (он зависит от миграций)."""
    return Job(**{name: row[name] for name in _JOB_FIELDS})


# Время сервера в unix-секундах. clock_timestamp(), а не now(): now() — начало транзакции.
_NOW = "EXTRACT(EPOCH FROM clock_timestamp())"
_SCHEMA = f"""
    CREATE TABLE IF NOT EXISTS jobs(
        id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, path TEXT NOT NULL, fingerprint TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        available_at DOUBLE PRECISION NOT NULL DEFAULT 0, error TEXT, progress TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'ingest', payload_json TEXT,
        created_at DOUBLE PRECISION DEFAULT {_NOW}, updated_at DOUBLE PRECISION DEFAULT {_NOW},
        started_at DOUBLE PRECISION, UNIQUE(path, fingerprint));
    CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(status, available_at, id);
    CREATE TABLE IF NOT EXISTS desired(path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS worker_heartbeat(name TEXT PRIMARY KEY, last_seen DOUBLE PRECISION NOT NULL)
"""
_MIGRATIONS = (
    # v2-v4: метки времени у очередей, созданных до того, как колонки попали в CREATE TABLE
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS created_at DOUBLE PRECISION, "
    "ADD COLUMN IF NOT EXISTS updated_at DOUBLE PRECISION, "
    "ADD COLUMN IF NOT EXISTS started_at DOUBLE PRECISION",
    f"ALTER TABLE jobs ALTER COLUMN created_at SET DEFAULT {_NOW}, "
    f"ALTER COLUMN updated_at SET DEFAULT {_NOW}",
    f"UPDATE jobs SET created_at=COALESCE(created_at,{_NOW}), updated_at=COALESCE(updated_at,{_NOW}) "
    "WHERE created_at IS NULL OR updated_at IS NULL",
    # v5: таблица write_state больше никем не читается
    "DROP TABLE IF EXISTS write_state",
    # v6: структурированный прогресс для сайта и скрытие завершённых задач из списка
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS stage TEXT, "
    "ADD COLUMN IF NOT EXISTS step_done INTEGER, ADD COLUMN IF NOT EXISTS step_total INTEGER, "
    "ADD COLUMN IF NOT EXISTS hidden BOOLEAN NOT NULL DEFAULT false",
)


class JobQueue:
    """Очередь задач одного проекта поверх одного долгоживущего соединения.

    Объект не потокобезопасен: у каждого потока (worker, watcher, запрос API) свой экземпляр.
    Соединение переживает рестарт PostgreSQL (`PgConnection`), но транзакция, прерванная
    обрывом, не повторяется — вызывающий получает `DatabaseError`."""

    def __init__(self, target: DatabaseTarget, *, clock=time.time):
        """Открывает соединение и создаёт/мигрирует таблицы очереди (`initialize`, под
        DDL-блокировкой). `clock` подменяется в тестах. При ошибке соединение закрывается."""
        self.target = target
        self._clock = clock
        self._db = connect(target)
        try:
            initialize(self._db, _SCHEMA, "queue", _MIGRATIONS)
        except BaseException:
            self._db.close()
            raise

    def close(self) -> None:
        """Закрывает соединение; незафиксированная транзакция откатывается."""
        self._db.close()

    def enqueue(
        self,
        path: Path,
        fingerprint: str,
        *,
        force: bool = False,
        kind: str = "ingest",
        payload_json: str | None = None,
        on_locked: Callable[[], None] | None = None,
    ) -> int:
        """Ставит в очередь операцию над `path` с версией `fingerprint` и возвращает id задачи.

        Всё выполняется в одной транзакции под блокировкой `queue-path:<путь>`; путь
        приводится к абсолютному (`resolve`), чтобы один файл не давал разных задач.

        Повтор той же версии не создаёт новую задачу. Уже завершённая (`done` / `failed` /
        `superseded` / `cancelled`) задача снова становится `pending` (попытки обнуляются) только с `force`
        или если до этого в `desired` была другая версия пути; `pending` и `running` задачи не
        трогаются. В конце `desired` пути = `fingerprint`.

        Источник, который забыли (`desired` = `remove:<та же версия>`) или чью загрузку отменили
        (`cancel:<та же версия>`), без `force` не возвращается: метод сразу отдаёт id той задачи.
        Так watcher не загрузит обратно файл, удалённый или отменённый через API. Задача,
        вернувшаяся в `pending`, снова видна в списке (`hidden` снимается).

        `on_locked` выполняется под блокировкой пути, до записи в таблицы: API переносит туда
        файл загрузки на место, чтобы идущая задача удаления не стёрла его следом. Если
        `on_locked` или запрос падает, транзакция откатывается и исключение пробрасывается."""
        path = str(path.resolve())
        self._db.execute("BEGIN")
        try:
            transaction_lock(self._db, f"queue-path:{path}")
            previous = self._db.execute(
                "SELECT fingerprint FROM desired WHERE path=?", (path,)
            ).fetchone()
            if (
                not force
                and previous is not None
                and previous[0]
                in (
                    f"remove:{fingerprint}",
                    f"cancel:{fingerprint}",
                )
            ):
                # забытый источник — задача удаления; отменённый — сама отменённая задача
                kept = previous[0] if previous[0].startswith("remove:") else fingerprint
                job_id = self._db.execute(
                    "SELECT id FROM jobs WHERE path=? AND fingerprint=?", (path, kept)
                ).fetchone()[0]
                self._db.execute("COMMIT")
                return job_id
            if on_locked is not None:
                on_locked()
            self._db.execute(
                "INSERT INTO jobs(path,fingerprint,kind,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(path,fingerprint) DO NOTHING",
                (path, fingerprint, kind, payload_json, self._clock(), self._clock()),
            )
            if force or (previous is not None and previous[0] != fingerprint):
                self._db.execute(
                    "UPDATE jobs SET status='pending',attempts=0,available_at=0,error=NULL,progress='',"
                    "stage=NULL,step_done=NULL,step_total=NULL,hidden=false,updated_at=?,started_at=NULL "
                    "WHERE path=? AND fingerprint=? AND status IN ('done','failed','superseded','cancelled')",
                    (self._clock(), path, fingerprint),
                )
            if fingerprint.startswith("remove:") and previous and previous[0] != fingerprint:
                self._remember_restore(path, fingerprint, previous[0])
            self._db.execute(
                "INSERT INTO desired VALUES(?,?) ON CONFLICT(path) DO UPDATE SET fingerprint=excluded.fingerprint",
                (path, fingerprint),
            )
            job_id = self._db.execute(
                "SELECT id FROM jobs WHERE path=? AND fingerprint=?", (path, fingerprint)
            ).fetchone()[0]
            self._db.execute("COMMIT")
            return job_id
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def claim(self, max_attempts: int | None = None) -> Job | None:
        """Атомарно берёт самую старую готовую `pending`-задачу (`available_at` наступил) и
        переводит её в `running`, увеличив `attempts`. Возвращает `None`, если брать нечего.

        `FOR UPDATE SKIP LOCKED` позволяет нескольким потребителям не мешать друг другу и не
        брать одну задачу дважды. С `max_attempts` задачи, уже исчерпавшие попытки, не берутся
        (их судьбу решает `fail` / `recover`)."""
        limit, args = ("", ()) if max_attempts is None else (" AND attempts<?", (max_attempts,))
        self._db.execute("BEGIN")
        try:
            row = self._db.execute(
                "UPDATE jobs SET status='running',attempts=attempts+1,progress='запуск',stage='start',"
                "step_done=NULL,step_total=NULL,started_at=?,updated_at=? "
                "WHERE id=(SELECT id FROM jobs WHERE status='pending' AND available_at<=?"
                + limit
                + " ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING *",
                (self._clock(), self._clock(), self._clock(), *args),
            ).fetchone()
            self._db.execute("COMMIT")
            return _job(row) if row else None
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def finish(self, job_id: int) -> None:
        """Отмечает `running`-задачу выполненной (`done`).

        Под блокировкой пути: если задача по-прежнему актуальна (её версия в `desired`), все
        прочие незавершённые и упавшие задачи того же пути становятся `superseded` — источник
        уже в нужном состоянии. Если задача уже не `running` (например, worker сам пометил её
        `superseded`), ничего не меняется."""
        self._db.execute("BEGIN")
        try:
            source = self._db.execute("SELECT path FROM jobs WHERE id=?", (job_id,)).fetchone()
            if source:
                transaction_lock(self._db, f"queue-path:{source[0]}")
            row = self._db.execute(
                "UPDATE jobs SET status='done',error=NULL,progress='завершено',updated_at=? "
                "WHERE id=? AND status='running' RETURNING path,fingerprint",
                (self._clock(), job_id),
            ).fetchone()
            if (
                row
                and self._db.execute(
                    "SELECT 1 FROM desired WHERE path=? AND fingerprint=?", row
                ).fetchone()
            ):
                self._db.execute(
                    "UPDATE jobs SET status='superseded',progress='текущая операция с источником завершена',updated_at=? "
                    "WHERE path=? AND id<>? AND status IN ('pending','running','failed')",
                    (self._clock(), row[0], job_id),
                )
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    @contextmanager
    def locked_path(self, path: str) -> Iterator[None]:
        """Транзакция с блокировкой `queue-path:<path>`: пока блок выполняется, ни `enqueue`,
        ни `finish`, ни загрузка через API не меняют этот путь.

        `path` должен быть уже абсолютным (как `Job.path`). Worker использует блок, чтобы
        проверить `is_current` и удалить файл загрузки атомарно относительно повторной
        загрузки. Исключение внутри блока откатывает транзакцию и пробрасывается."""
        self._db.execute("BEGIN")
        try:
            transaction_lock(self._db, f"queue-path:{path}")
            yield
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def clear(self) -> None:
        """Забывает всю историю очереди (`jobs` и `desired`) для `reset`: после него те же
        файлы снова загружаются, а не считаются уже выполненными или удалёнными.
        `worker_heartbeat` не трогается. Блокировки путей не берутся: вызывающий держит
        `ingest_lock`, поэтому worker в это время задачи не выполняет."""
        with self._db:
            self._db.execute("DELETE FROM jobs")
            self._db.execute("DELETE FROM desired")

    def is_current(self, job: Job) -> bool:
        """`True`, если версия задачи — последняя запрошенная для её пути (`desired`).

        Без блокировки ответ может устареть сразу после возврата; когда это важно, вызывать
        внутри `locked_path`."""
        return (
            self._db.execute(
                "SELECT 1 FROM desired WHERE path=? AND fingerprint=?",
                (job.path, job.fingerprint),
            ).fetchone()
            is not None
        )

    def cancel(self, job_id: int, *, on_locked: Callable[[Job], None] | None = None) -> Job | None:
        """Отменяет `pending`-задачу и возвращает её новое состояние; `None` — задачи нет или
        она уже не `pending` (worker взял её первым, или она завершена).

        Одна транзакция под блокировкой `queue-path:<путь>`. `desired` пути меняется, только
        если в нём всё ещё версия задачи: отменённое удаление `remove:<v>` возвращает `<v>`
        (документ снова актуален), отменённая загрузка `<v>` даёт `cancel:<v>` — без `force`
        `enqueue` её не вернёт. `on_locked(job)` выполняется под той же блокировкой до COMMIT
        (API удаляет там файл загрузки); его исключение откатывает отмену и пробрасывается."""
        self._db.execute("BEGIN")
        try:
            source = self._db.execute("SELECT path FROM jobs WHERE id=?", (job_id,)).fetchone()
            if source is None:
                self._db.execute("COMMIT")
                return None
            transaction_lock(self._db, f"queue-path:{source[0]}")
            row = self._db.execute(
                "UPDATE jobs SET status='cancelled',progress='отменена',stage=NULL,step_done=NULL,"
                "step_total=NULL,available_at=0,updated_at=? WHERE id=? AND status='pending' "
                "RETURNING *",
                (self._clock(), job_id),
            ).fetchone()
            if row is None:
                self._db.execute("COMMIT")
                return None
            job = _job(row)
            self._restore_desired(job)
            if on_locked is not None:
                on_locked(job)
            self._db.execute("COMMIT")
            return job
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def cancel_removal(self, fingerprint: str) -> list[Job] | None:
        """Отменяет удаление документа на всех копиях сразу (`fingerprint` = `remove:<v>`) и
        возвращает отменённые задачи; `None` — какая-то копия уже выполняется, тогда не
        отменяется ничего (иначе часть копий забылась бы, а часть осталась).

        Одна транзакция: блокировки путей всех копий (по порядку), затем `FOR UPDATE` строк
        задач — `claim` их пропускает (`SKIP LOCKED`), так что worker не возьмёт копию между
        проверкой и отменой."""
        self._db.execute("BEGIN")
        try:
            paths = self._db.execute(
                "SELECT DISTINCT path FROM jobs WHERE fingerprint=? "
                "AND status IN ('pending','running') ORDER BY path",
                (fingerprint,),
            ).fetchall()
            for (path,) in paths:
                transaction_lock(self._db, f"queue-path:{path}")
            statuses = [
                r[0]
                for r in self._db.execute(
                    "SELECT status FROM jobs WHERE fingerprint=? "
                    "AND status IN ('pending','running') FOR UPDATE",
                    (fingerprint,),
                )
            ]
            if "running" in statuses:
                self._db.execute("COMMIT")
                return None
            rows = self._db.execute(
                "UPDATE jobs SET status='cancelled',progress='отменена',stage=NULL,step_done=NULL,"
                "step_total=NULL,available_at=0,updated_at=? WHERE fingerprint=? AND status='pending' "
                "RETURNING *",
                (self._clock(), fingerprint),
            ).fetchall()
            jobs = sorted((_job(r) for r in rows), key=lambda j: j.id)
            for job in jobs:
                self._restore_desired(job)
            self._db.execute("COMMIT")
            return jobs
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def _remember_restore(self, path: str, fingerprint: str, previous: str) -> None:
        """Запоминает в `payload_json` задачи удаления, что было в `desired` до неё (`restore`):
        отмена удаления вернёт именно это (новую ревизию файла или `cancel:<v>`), а не голую
        версию документа. Внутри транзакции `enqueue`."""
        row = self._db.execute(
            "SELECT payload_json FROM jobs WHERE path=? AND fingerprint=?", (path, fingerprint)
        ).fetchone()
        data = json.loads(row[0] or "{}") if row else {}
        data["restore"] = previous
        self._db.execute(
            "UPDATE jobs SET payload_json=? WHERE path=? AND fingerprint=?",
            (json.dumps(data, ensure_ascii=False), path, fingerprint),
        )

    def _restore_desired(self, job: Job) -> None:
        """`desired` пути после отмены, если в нём всё ещё версия задачи: удаление
        `remove:<v>` возвращает то, что было до него (`restore` в `payload_json`, иначе `<v>`),
        загрузка `<v>` даёт `cancel:<v>`. Внутри транзакции."""
        if job.fingerprint.startswith("remove:"):
            saved = json.loads(job.payload_json or "{}").get("restore")
            restored = saved or job.fingerprint.removeprefix("remove:")
        else:
            restored = f"cancel:{job.fingerprint}"
        self._db.execute(
            "UPDATE desired SET fingerprint=? WHERE path=? AND fingerprint=?",
            (restored, job.path, job.fingerprint),
        )

    def supersede(self, job_id: int) -> None:
        """Переводит `running`-задачу в `superseded`: worker обнаружил, что файл изменился, и
        уже поставил задачу на новую версию."""
        self._db.execute(
            "UPDATE jobs SET status='superseded',progress='в очередь поставлено новое содержимое',updated_at=? "
            "WHERE id=? AND status='running'",
            (self._clock(), job_id),
        )

    def defer(self, job_id: int, delay: float) -> None:
        """Откладывает `pending`-задачу на `delay` секунд от текущего момента (например, чтобы
        дать файлу «успокоиться» после записи). Другие статусы не меняются."""
        self._db.execute(
            "UPDATE jobs SET available_at=? WHERE id=? AND status='pending'",
            (self._clock() + delay, job_id),
        )

    def fail(self, job_id: int, error: str, *, max_attempts: int, delay: float) -> None:
        """Засчитывает неудачную попытку `running`-задачи (ошибка самого документа).

        Если попыток уже `max_attempts` или больше — статус `failed`, иначе `pending` с
        экспоненциальной задержкой `delay * 2^(attempts-1)` секунд, не больше часа (показатель
        степени ограничен 16, чтобы не переполнить float). `error` хранится как есть, поэтому
        сюда передают только имя типа исключения. Задача, которая уже не `running`, не
        меняется."""
        job = self._db.execute("SELECT attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
        attempts = job[0]
        self._db.execute(
            "UPDATE jobs SET status=?,available_at=?,error=?,progress='попытка не удалась',updated_at=?,started_at=NULL "
            "WHERE id=? AND status='running'",
            (
                "failed" if attempts >= max_attempts else "pending",
                self._clock() + min(delay * 2 ** min(attempts - 1, 16), 3600),
                error,
                self._clock(),
                job_id,
            ),
        )

    def release(self, job_id: int, delay: float) -> None:
        """Возвращает `running`-задачу в `pending`, не засчитывая попытку: сбой
        инфраструктуры (база, Neo4j, сервер моделей), а не файла. `attempts`, увеличенный в
        `claim`, уменьшается обратно; повтор не раньше чем через `delay` секунд, без
        удвоения — инфраструктура может лежать долго, и задача не должна стать `failed`."""
        self._db.execute(
            "UPDATE jobs SET status='pending',attempts=GREATEST(attempts-1,0),available_at=?,"
            "progress='ожидание сервисов',updated_at=?,started_at=NULL WHERE id=? AND status='running'",
            (self._clock() + delay, self._clock(), job_id),
        )

    def recover(self, max_attempts: int | None = None) -> int:
        """Возвращает в очередь задачи, оставшиеся `running` после краха или остановки
        worker'а, и возвращает их число.

        Задача, которая уже исчерпала `max_attempts` попыток (например, файл роняет процесс),
        становится `failed` с ошибкой `InterruptedTooOften`, а не берётся снова бесконечно.
        Остальные становятся `pending` и готовы сразу; `attempts` не уменьшается — прерванный
        запуск тоже считается попыткой.

        Вызывать только под блокировкой `worker` (`database_lock(settings, "worker")`):
        иначе можно отобрать задачи у другого, ещё работающего worker'а."""
        if max_attempts is not None:
            self._db.execute(
                "UPDATE jobs SET status='failed',error='InterruptedTooOften',"
                "progress='слишком много прерываний',updated_at=?,started_at=NULL "
                "WHERE status='running' AND attempts>=?",
                (self._clock(), max_attempts),
            )
        return self._db.execute(
            "UPDATE jobs SET status='pending',available_at=0,progress='возобновление после прерывания',updated_at=?,started_at=NULL "
            "WHERE status='running'",
            (self._clock(),),
        ).rowcount

    def retry(self, job_id: int | None = None) -> int:
        """Перезапускает `failed`-задачи (одну по `job_id` или все) и возвращает их число.

        Задача становится `pending` с нулевыми попытками, а её версия снова записывается в
        `desired` — владелец явно попросил повторить именно её. Блокировки путей берутся в
        отсортированном порядке, чтобы два параллельных `retry` не взаимоблокировались."""
        clause, args = (" AND id=?", (job_id,)) if job_id is not None else ("", ())
        self._db.execute("BEGIN")
        try:
            selected = self._db.execute(
                "SELECT id,path,fingerprint FROM jobs WHERE status='failed'"
                + clause
                + " ORDER BY id",
                args,
            ).fetchall()
            for path in sorted({row[1] for row in selected}):
                transaction_lock(self._db, f"queue-path:{path}")
            count = 0
            # Повторяем только выбранные выше id: задача, упавшая уже после выборки,
            # относится к следующему retry, и её состояние не сбрасывается случайно.
            for selected_id, _, _ in selected:
                row = self._db.execute(
                    "UPDATE jobs SET status='pending',attempts=0,available_at=0,error=NULL,progress='',"
                    "stage=NULL,step_done=NULL,step_total=NULL,hidden=false,updated_at=?,started_at=NULL "
                    "WHERE id=? AND status='failed' RETURNING path,fingerprint",
                    (self._clock(), selected_id),
                ).fetchone()
                if row:
                    count += 1
                    self._db.execute(
                        "INSERT INTO desired VALUES(?,?) ON CONFLICT(path) DO UPDATE SET fingerprint=excluded.fingerprint",
                        row,
                    )
            self._db.execute("COMMIT")
            return count
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def set_progress(
        self,
        job_id: int,
        progress: str,
        *,
        stage: str | None = None,
        done: int | None = None,
        total: int | None = None,
    ) -> None:
        """Записывает прогресс задачи: текст для `jobs list` и шаг (`stage`, счётчики
        `done` / `total`, None — неизвестно) для сайта."""
        self._db.execute(
            "UPDATE jobs SET progress=?,stage=?,step_done=?,step_total=? WHERE id=?",
            (progress, stage, done, total, job_id),
        )

    def get(self, job_id: int) -> Job | None:
        """Задача по id или `None`."""
        row = self._db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _job(row) if row else None

    def jobs(self, limit: int = 100, offset: int = 0, *, include_hidden: bool = False) -> list[Job]:
        """Страница задач, новые первыми (`ORDER BY id DESC`); скрытые `hide_finished`
        задачи — только с `include_hidden`."""
        where = "" if include_hidden else " WHERE NOT hidden"
        return [
            _job(r)
            for r in self._db.execute(
                f"SELECT * FROM jobs{where} ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
            )
        ]

    def pending_ids(self, fingerprint: str) -> list[int]:
        """id `pending`-задач с версией `fingerprint` (например, все копии `remove:<doc_id>`),
        по возрастанию."""
        rows = self._db.execute(
            "SELECT id FROM jobs WHERE fingerprint=? AND status='pending' ORDER BY id",
            (fingerprint,),
        )
        return [r[0] for r in rows]

    def has_ready(self, kind: str, max_attempts: int | None = None) -> bool:
        """Есть ли готовая к работе `pending`-задача вида `kind` (как их отбирает `claim`).

        Worker по нему откладывает сборку wiki, пока за текущей загрузкой ждут другие."""
        limit, args = ("", ()) if max_attempts is None else (" AND attempts<?", (max_attempts,))
        row = self._db.execute(
            "SELECT 1 FROM jobs WHERE status='pending' AND kind=? AND available_at<=?"
            + limit
            + " LIMIT 1",
            (kind, self._clock(), *args),
        ).fetchone()
        return row is not None

    def hide_finished(self) -> int:
        """Скрывает из `jobs()` завершённые задачи (`done`, `superseded`, `cancelled`) и
        возвращает их число. `failed` не скрываются: у них «Повторить», это сигнал о поломке.
        Строки остаются — на них опираются `enqueue` (забытые и отменённые источники) и поиск
        дубликатов загрузок."""
        return self._db.execute(
            "UPDATE jobs SET hidden=true "
            "WHERE status IN ('done','superseded','cancelled') AND NOT hidden"
        ).rowcount

    def find(self, path: Path, fingerprint: str) -> Job | None:
        """Задача для пути и версии источника (пара уникальна), если она есть."""
        row = self._db.execute(
            "SELECT * FROM jobs WHERE path=? AND fingerprint=?", (str(path.resolve()), fingerprint)
        ).fetchone()
        return _job(row) if row else None

    def running(self) -> list[Job]:
        """Все выполняемые сейчас задачи (для статуса; их не больше числа worker'ов)."""
        return [_job(r) for r in self._db.execute("SELECT * FROM jobs WHERE status='running'")]

    def counts(self, *, include_hidden: bool = True) -> dict[str, int]:
        """Число задач по статусам: `{"pending": 3, "done": 10, ...}`; нулевых нет. Без
        `include_hidden` скрытые `hide_finished` задачи не считаются (как в `jobs()`)."""
        where = "" if include_hidden else " WHERE NOT hidden"
        return dict(self._db.execute(f"SELECT status,COUNT(*) FROM jobs{where} GROUP BY status"))

    def heartbeat(self) -> None:
        """Записывает «worker жив сейчас» в `worker_heartbeat`. Worker вызывает это в цикле
        и при каждом шаге прогресса; `/api/status` считает worker мёртвым, если отметки нет
        дольше `HEARTBEAT_STALE_S`."""
        self._db.execute(
            "INSERT INTO worker_heartbeat VALUES('worker',?) "
            "ON CONFLICT(name) DO UPDATE SET last_seen=excluded.last_seen",
            (self._clock(),),
        )

    def last_heartbeat(self) -> float | None:
        """Unix-время последней отметки worker'а или `None`, если worker ни разу не запускался."""
        row = self._db.execute(
            "SELECT last_seen FROM worker_heartbeat WHERE name='worker'"
        ).fetchone()
        return row[0] if row else None
