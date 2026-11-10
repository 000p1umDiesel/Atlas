"""Загруженные через API документы: исходное имя ↔ doc_id ↔ файл в каталоге загрузок."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from mnogobase.config import Settings
from mnogobase.database import connect, database_target, initialize

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS uploads("
    "doc_id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL)"
)


class Uploads:
    def __init__(self, settings: Settings):
        self.root = settings.service.uploads_dir.resolve()
        self._target = database_target(settings)
        with self._db() as db:
            initialize(db, _SCHEMA, "uploads")

    @contextmanager
    def _db(self):
        db = connect(self._target)
        try:
            yield db
        finally:
            db.close()

    def contained(self, path: Path, *, must_exist: bool = True) -> Path:
        """Путь внутри каталога загрузок, без симлинков; иначе FileNotFoundError."""
        resolved = path.resolve()
        if (
            path.is_symlink()
            or not resolved.is_relative_to(self.root)
            or (must_exist and not resolved.is_file())
        ):
            raise FileNotFoundError("Source unavailable")
        return resolved

    def get(self, doc_id: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM uploads WHERE doc_id=?", (doc_id,)).fetchone()
        return dict(row) if row else None

    def list(self, limit: int, offset: int) -> list[dict]:
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM uploads ORDER BY doc_id LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
        return [dict(r) for r in rows]

    def add(self, doc_id: str, name: str, path: Path) -> None:
        with self._db() as db, db:
            db.execute(
                "INSERT INTO uploads VALUES(?,?,?) ON CONFLICT(doc_id) DO NOTHING",
                (doc_id, name, str(path.resolve())),
            )

    def delete(self, doc_id: str) -> None:
        with self._db() as db, db:
            db.execute("DELETE FROM uploads WHERE doc_id=?", (doc_id,))
