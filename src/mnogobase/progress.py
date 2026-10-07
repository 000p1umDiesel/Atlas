"""Progress reporting for long runs (ingest, wiki build, reindex).

The library only calls a `ProgressSink`; drawing it is the CLI's job. A run reports:

- `files_found(total)` once, then per file `file_started(path)` ... `file_done(status)`;
- `step(name, total)` when a step starts: a stage of the current file (parse, chunk, embed,
  extract, graph) or a step of the whole run (wiki evidence / draft / write, reindex ...).
  `total` is the number of units (chunks, entities, pages) when known; a step may be
  announced again with its total once that is known;
- `advance(n, failed=...)` as units of the current step finish, failed ones included.

All calls come from the thread running the event loop (or the only thread)."""

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
    """Reports nothing: the default for library callers and tests."""

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
