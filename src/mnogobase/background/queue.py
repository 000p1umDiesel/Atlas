"""Short atomic SQLite transactions; callers own worker/writer file locks."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Job:
    id: int
    path: str
    fingerprint: str
    status: str
    attempts: int
    available_at: float
    error: str | None
    progress: str


class JobQueue:
    def __init__(self, path: Path, *, clock=time.time):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs(
                id INTEGER PRIMARY KEY, path TEXT NOT NULL, fingerprint TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                available_at REAL NOT NULL DEFAULT 0, error TEXT, progress TEXT NOT NULL DEFAULT '',
                UNIQUE(path, fingerprint));
            CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(status, available_at, id);
            CREATE TABLE IF NOT EXISTS desired(path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL);
        """)

    def close(self) -> None:
        self._db.close()

    def enqueue(self, path: Path, fingerprint: str, *, force: bool = False) -> int:
        path = str(path.resolve())
        self._db.execute("BEGIN IMMEDIATE")
        try:
            previous = self._db.execute(
                "SELECT fingerprint FROM desired WHERE path=?", (path,)
            ).fetchone()
            self._db.execute(
                "INSERT OR IGNORE INTO jobs(path,fingerprint) VALUES(?,?)", (path, fingerprint)
            )
            if force or (previous is not None and previous[0] != fingerprint):
                self._db.execute(
                    "UPDATE jobs SET status='pending',attempts=0,available_at=0,error=NULL,progress='' "
                    "WHERE path=? AND fingerprint=? AND status IN ('done','failed','superseded')",
                    (path, fingerprint),
                )
            self._db.execute("INSERT OR REPLACE INTO desired VALUES(?,?)", (path, fingerprint))
            job_id = self._db.execute(
                "SELECT id FROM jobs WHERE path=? AND fingerprint=?", (path, fingerprint)
            ).fetchone()[0]
            self._db.execute("COMMIT")
            return job_id
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def claim(self) -> Job | None:
        row = self._db.execute(
            "UPDATE jobs SET status='running',attempts=attempts+1,progress='starting' "
            "WHERE id=(SELECT id FROM jobs WHERE status='pending' AND available_at<=? "
            "ORDER BY id LIMIT 1) RETURNING *",
            (self._clock(),),
        ).fetchone()
        return Job(*row) if row else None

    def finish(self, job_id: int) -> None:
        self._db.execute(
            "UPDATE jobs SET status='done',error=NULL,progress='completed' "
            "WHERE id=? AND status='running'",
            (job_id,),
        )

    def supersede(self, job_id: int) -> None:
        self._db.execute(
            "UPDATE jobs SET status='superseded',progress='new content queued' "
            "WHERE id=? AND status='running'",
            (job_id,),
        )

    def defer(self, job_id: int, delay: float) -> None:
        self._db.execute(
            "UPDATE jobs SET available_at=? WHERE id=? AND status='pending'",
            (self._clock() + delay, job_id),
        )

    def fail(self, job_id: int, error: str, *, max_attempts: int, delay: float) -> None:
        job = self._db.execute("SELECT attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
        attempts = job[0]
        self._db.execute(
            "UPDATE jobs SET status=?,available_at=?,error=?,progress='failed attempt' "
            "WHERE id=? AND status='running'",
            (
                "failed" if attempts >= max_attempts else "pending",
                self._clock() + min(delay * 2 ** min(attempts - 1, 16), 3600),
                error,
                job_id,
            ),
        )

    def recover(self) -> int:
        # Only while holding worker.lock: never steal another worker's active jobs.
        return self._db.execute(
            "UPDATE jobs SET status='pending',available_at=0,progress='resuming after interruption' "
            "WHERE status='running'"
        ).rowcount

    def retry(self, job_id: int | None = None) -> int:
        clause, args = (" AND id=?", (job_id,)) if job_id is not None else ("", ())
        return self._db.execute(
            "UPDATE jobs SET status='pending',attempts=0,available_at=0,error=NULL,progress='' "
            "WHERE status='failed'" + clause,
            args,
        ).rowcount

    def set_progress(self, job_id: int, progress: str) -> None:
        self._db.execute("UPDATE jobs SET progress=? WHERE id=?", (progress, job_id))

    def jobs(self, limit: int = 100) -> list[Job]:
        return [
            Job(*r)
            for r in self._db.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))
        ]

    def counts(self) -> dict[str, int]:
        return dict(self._db.execute("SELECT status,COUNT(*) FROM jobs GROUP BY status"))
