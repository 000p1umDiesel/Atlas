"""Отчёт о прогрессе долгих запусков (ingest, wiki build, reindex).

Библиотека только вызывает `ProgressSink`; отрисовка — задача CLI. Запуск сообщает:

- `files_found(total)` один раз, затем для каждого файла `file_started(path)` ...
  `file_done(status)`;
- `step(name, total)` при старте шага: стадии текущего файла (parse, chunk, embed,
  extract, graph) или шага всего запуска (wiki evidence / draft / write, reindex ...).
  `total` — число единиц (чанков, сущностей, страниц), если оно известно; шаг можно
  объявить повторно с total, когда оно станет известно;
- `advance(n, failed=...)` по мере завершения единиц текущего шага, включая упавшие.

Все вызовы приходят из потока, где крутится event loop (или из единственного потока)."""

from __future__ import annotations

from typing import Protocol


class ProgressSink(Protocol):
    def files_found(self, total: int) -> None: ...

    def file_started(self, path: str) -> None: ...

    def file_done(self, status: str) -> None:
        """`status`: processed | skipped | failed."""

    def step(self, name: str, total: int | None = None) -> None: ...

    def advance(self, n: int = 1, *, failed: bool = False) -> None: ...


class NullProgress:
    """Ничего не сообщает: вариант по умолчанию для вызовов из библиотеки и тестов."""

    def files_found(self, total: int) -> None:
        pass

    def file_started(self, path: str) -> None:
        pass

    def file_done(self, status: str) -> None:
        pass

    def step(self, name: str, total: int | None = None) -> None:
        pass

    def advance(self, n: int = 1, *, failed: bool = False) -> None:
        pass


NULL_PROGRESS = NullProgress()
