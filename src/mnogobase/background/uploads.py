"""Загруженные через API документы: исходное имя ↔ doc_id ↔ файл в каталоге загрузок.

API сохраняет загрузку под именем `<doc_id><расширение>` в `service.uploads_dir`, а
настоящее имя файла держит в таблице `uploads` (схема проекта). По ней API показывает
документы и источники ответов под исходными именами и отдаёт содержимое
(`/api/documents/{doc_id}/content`), а worker после задачи `remove` удаляет файл и строку.
Документы, добавленные из CLI или папки, здесь не записываются: их файлы владельцу
принадлежат и никогда не удаляются.
"""

from __future__ import annotations

from pathlib import Path

from mnogobase.core.config import Settings
from mnogobase.core.database import connection, database_target, initialize

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS uploads("
    "doc_id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL)"
)


class Uploads:
    """Таблица `uploads` и проверка путей внутри каталога загрузок.

    Каждый метод открывает короткое соединение из пула, поэтому объект можно использовать
    из разных потоков."""

    def __init__(self, settings: Settings):
        """Запоминает абсолютный `service.uploads_dir` и создаёт таблицу `uploads`, если её
        нет (запись в базу). Сам каталог не создаётся."""
        self.root = settings.service.uploads_dir.resolve()
        self._target = database_target(settings)
        with connection(self._target) as db:
            initialize(db, _SCHEMA, "uploads")

    def contained(self, path: Path, *, must_exist: bool = True) -> Path:
        """Возвращает абсолютный путь, если `path` лежит внутри каталога загрузок и не
        является символической ссылкой; с `must_exist` файл ещё и должен существовать.

        Иначе `FileNotFoundError` с нейтральным текстом: так путь из базы или из задачи не
        может указать на файл вне каталога загрузок (защита от удаления и выдачи чужих
        файлов)."""
        resolved = path.resolve()
        if (
            path.is_symlink()
            or not resolved.is_relative_to(self.root)
            or (must_exist and not resolved.is_file())
        ):
            raise FileNotFoundError("Source unavailable")
        return resolved

    def get(self, doc_id: str) -> dict | None:
        """Запись `{"doc_id", "name", "path"}` загрузки или `None`."""
        with connection(self._target) as db:
            row = db.execute("SELECT * FROM uploads WHERE doc_id=?", (doc_id,)).fetchone()
        return dict(row) if row else None

    def add(self, doc_id: str, name: str, path: Path) -> None:
        """Записывает загрузку. Уже известный `doc_id` не перезаписывается: то же содержимое
        под другим именем остаётся под первым именем и в прежнем файле."""
        with connection(self._target) as db, db:
            db.execute(
                "INSERT INTO uploads VALUES(?,?,?) ON CONFLICT(doc_id) DO NOTHING",
                (doc_id, name, str(path.resolve())),
            )

    def delete(self, doc_id: str) -> None:
        """Удаляет запись загрузки (файл удаляет вызывающий)."""
        with connection(self._target) as db, db:
            db.execute("DELETE FROM uploads WHERE doc_id=?", (doc_id,))
