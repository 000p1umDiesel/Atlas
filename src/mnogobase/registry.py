from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from filelock import FileLock

STAGES: tuple[str, ...] = ("parse", "chunk", "embed", "extract", "graph")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
    path TEXT PRIMARY KEY, doc_id TEXT NOT NULL, size INTEGER, mtime REAL,
    status TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS files_doc ON files(doc_id);
CREATE TABLE IF NOT EXISTS stages(
    doc_id TEXT, stage TEXT, status TEXT, attempts INTEGER DEFAULT 0, error TEXT,
    updated_at TEXT, PRIMARY KEY(doc_id, stage));
CREATE TABLE IF NOT EXISTS chunk_extract(
    chunk_id TEXT PRIMARY KEY, status TEXT, attempts INTEGER DEFAULT 0, error TEXT);
CREATE TABLE IF NOT EXISTS extraction_cache(
    chunk_id TEXT, prompt_version TEXT, model TEXT, result_json TEXT,
    PRIMARY KEY(chunk_id, prompt_version, model));
CREATE TABLE IF NOT EXISTS dirty_entities(entity_id TEXT PRIMARY KEY, marked_at TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS pending_removals(doc_id TEXT PRIMARY KEY, plan_json TEXT NOT NULL);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class FileRow:
    path: str
    doc_id: str
    size: int
    mtime: float
    status: str


class Registry:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    # ---- файлы ----
    def get_file(self, path: str) -> FileRow | None:
        row = self._db.execute(
            "SELECT path, doc_id, size, mtime, status FROM files WHERE path=?", (path,)
        ).fetchone()
        return FileRow(*row) if row else None

    def upsert_file(self, path: str, doc_id: str, size: int, mtime: float, status: str) -> None:
        self._db.execute(
            "INSERT INTO files(path, doc_id, size, mtime, status, updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET doc_id=excluded.doc_id, size=excluded.size, "
            "mtime=excluded.mtime, status=excluded.status, updated_at=excluded.updated_at",
            (path, doc_id, size, mtime, status, _now()),
        )

    def set_file_status(self, path: str, status: str) -> None:
        self._db.execute(
            "UPDATE files SET status=?, updated_at=? WHERE path=?", (status, _now(), path)
        )

    def drop_file(self, path: str) -> None:
        self._db.execute("DELETE FROM files WHERE path=?", (path,))

    def files(self) -> list[FileRow]:
        rows = self._db.execute(
            "SELECT path, doc_id, size, mtime, status FROM files ORDER BY path"
        ).fetchall()
        return [FileRow(*r) for r in rows]

    def failed_paths(self) -> list[str]:
        rows = self._db.execute("SELECT path FROM files WHERE status='failed' ORDER BY path")
        return [r[0] for r in rows]

    def paths_for_doc(self, doc_id: str) -> list[str]:
        rows = self._db.execute("SELECT path FROM files WHERE doc_id=? ORDER BY path", (doc_id,))
        return [r[0] for r in rows]

    def doc_ids(self) -> list[str]:
        rows = self._db.execute(
            "SELECT DISTINCT doc_id FROM files WHERE status='done' ORDER BY doc_id"
        )
        return [r[0] for r in rows]

    # ---- стадии ----
    def stage_status(self, doc_id: str, stage: str) -> str:
        row = self._db.execute(
            "SELECT status FROM stages WHERE doc_id=? AND stage=?", (doc_id, stage)
        ).fetchone()
        return row[0] if row else "pending"

    def set_stage(self, doc_id: str, stage: str, status: str, error: str | None = None) -> None:
        started = 1 if status == "running" else 0
        self._db.execute(
            "INSERT INTO stages(doc_id, stage, status, attempts, error, updated_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(doc_id, stage) DO UPDATE SET "
            "status=excluded.status, error=excluded.error, updated_at=excluded.updated_at, "
            "attempts=stages.attempts + ?",
            (doc_id, stage, status, started, error, _now(), started),
        )

    def pending_stages(self, doc_id: str) -> list[str]:
        return [s for s in STAGES if self.stage_status(doc_id, s) != "done"]

    def reset_running(self) -> int:
        cur = self._db.execute("UPDATE stages SET status='pending' WHERE status='running'")
        return cur.rowcount

    def stage_errors(self) -> list[tuple[str, str, str]]:
        rows = self._db.execute(
            "SELECT doc_id, stage, error FROM stages WHERE status='failed' ORDER BY doc_id, stage"
        )
        return [tuple(r) for r in rows]

    def stage_summary(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        rows = self._db.execute("SELECT stage, status, COUNT(*) FROM stages GROUP BY stage, status")
        for stage, status, n in rows:
            out.setdefault(stage, {})[status] = n
        return out

    def reset_stages(self, doc_id: str) -> None:
        """Сбрасывает статусы стадий документа (он ingest-ится заново); кэши сохраняются."""
        self._db.execute("DELETE FROM stages WHERE doc_id=?", (doc_id,))

    def clear_doc(self, doc_id: str) -> None:
        prefix = f"{doc_id}:%"
        self._db.execute("DELETE FROM stages WHERE doc_id=?", (doc_id,))
        self._db.execute("DELETE FROM chunk_extract WHERE chunk_id LIKE ?", (prefix,))
        self._db.execute("DELETE FROM extraction_cache WHERE chunk_id LIKE ?", (prefix,))

    # ---- извлечение по чанкам ----
    def set_chunk_extract(self, chunk_id: str, status: str, error: str | None = None) -> None:
        self._db.execute(
            "INSERT INTO chunk_extract(chunk_id, status, attempts, error) VALUES(?,?,1,?) "
            "ON CONFLICT(chunk_id) DO UPDATE SET status=excluded.status, error=excluded.error, "
            "attempts=chunk_extract.attempts + 1",
            (chunk_id, status, error),
        )

    def chunk_extract_counts(self, doc_id: str) -> dict[str, int]:
        rows = self._db.execute(
            "SELECT status, COUNT(*) FROM chunk_extract WHERE chunk_id LIKE ? GROUP BY status",
            (f"{doc_id}:%",),
        )
        return {status: n for status, n in rows}

    def get_extraction(self, chunk_id: str, prompt_version: str, model: str) -> str | None:
        row = self._db.execute(
            "SELECT result_json FROM extraction_cache "
            "WHERE chunk_id=? AND prompt_version=? AND model=?",
            (chunk_id, prompt_version, model),
        ).fetchone()
        return row[0] if row else None

    def get_latest_extraction(self, chunk_id: str) -> str | None:
        """Последнее записанное извлечение для чанка, независимо от модели и промпта."""
        row = self._db.execute(
            "SELECT result_json FROM extraction_cache WHERE chunk_id=? ORDER BY rowid DESC LIMIT 1",
            (chunk_id,),
        ).fetchone()
        return row[0] if row else None

    def latest_extraction_versions(self) -> set[str]:
        """Версии промпта последнего записанного извлечения для каждого чанка."""
        rows = self._db.execute(
            "SELECT DISTINCT prompt_version FROM extraction_cache WHERE rowid IN "
            "(SELECT MAX(rowid) FROM extraction_cache GROUP BY chunk_id)"
        )
        return {r[0] for r in rows}

    def put_extraction(
        self, chunk_id: str, prompt_version: str, model: str, result_json: str
    ) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO extraction_cache(chunk_id, prompt_version, model, result_json) "
            "VALUES(?,?,?,?)",
            (chunk_id, prompt_version, model, result_json),
        )

    # ---- dirty-сущности ----
    def mark_dirty(self, entity_ids: Iterable[str]) -> None:
        now = _now()
        self._db.executemany(
            "INSERT OR REPLACE INTO dirty_entities(entity_id, marked_at) VALUES(?, ?)",
            [(e, now) for e in set(entity_ids)],
        )

    def dirty(self) -> list[str]:
        rows = self._db.execute("SELECT entity_id FROM dirty_entities ORDER BY entity_id")
        return [r[0] for r in rows]

    def clear_dirty(self, entity_ids: Iterable[str]) -> None:
        self._db.executemany(
            "DELETE FROM dirty_entities WHERE entity_id=?", [(e,) for e in set(entity_ids)]
        )

    # ---- незавершённые удаления документов (журнал: доделываются после сбоя) ----
    def put_removal(self, doc_id: str, plan_json: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO pending_removals(doc_id, plan_json) VALUES(?, ?)",
            (doc_id, plan_json),
        )

    def get_removal(self, doc_id: str) -> str | None:
        row = self._db.execute(
            "SELECT plan_json FROM pending_removals WHERE doc_id=?", (doc_id,)
        ).fetchone()
        return row[0] if row else None

    def pending_removals(self) -> list[tuple[str, str]]:
        rows = self._db.execute("SELECT doc_id, plan_json FROM pending_removals ORDER BY doc_id")
        return [(r[0], r[1]) for r in rows]

    def drop_removal(self, doc_id: str) -> None:
        self._db.execute("DELETE FROM pending_removals WHERE doc_id=?", (doc_id,))

    # ---- meta ----
    def get_meta(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))

    def replace_meta(self, key: str, expected: str | None, value: str) -> bool:
        """Записывает `value` в `key`, только если там всё ещё `expected` (None: значения
        пока нет), чтобы не затереть значение, записанное тем временем другим процессом.
        Возвращает, было ли значение записано."""
        if expected is None:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES(?, ?)", (key, value)
            )
        else:
            cursor = self._db.execute(
                "UPDATE meta SET value=? WHERE key=? AND value=?", (value, key, expected)
            )
        return cursor.rowcount == 1

    def wipe(self) -> None:
        for table in (
            "files",
            "stages",
            "chunk_extract",
            "extraction_cache",
            "dirty_entities",
            "meta",
            "pending_removals",
        ):
            self._db.execute(f"DELETE FROM {table}")


def ingest_lock(data_dir: Path) -> FileLock:
    data_dir.mkdir(parents=True, exist_ok=True)
    return FileLock(data_dir / "ingest.lock", timeout=0)
