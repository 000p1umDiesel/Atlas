"""Вывод команд в терминал: живой прогресс долгих операций, ответы со ссылками на источники
и сводки построения wiki.

Весь текст документов и ответов LLM печатается через `escape(...)`: квадратные скобки в нём
(например, ссылки `[1]`) не должны разбираться как разметка Rich.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from rich.markup import escape
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    Task,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text

from mnogobase.cli import common
from mnogobase.core.models import Answer
from mnogobase.ops.doctor import Check
from mnogobase.wiki.builder import WikiReport


def source_label(path: str | None, page: int | None, ref: str) -> str:
    """Короткая подпись источника: имя файла (или ref, если пути нет) и страница."""
    where = Path(path).name if path else ref
    return f"{where}, p.{page}" if page else where


def print_answer(answer: Answer) -> None:
    """Ответ в рамке и таблица источников: отмечены те, на которые ответ ссылается."""
    common.console.print(
        Panel(escape(answer.text), title=f"{answer.mode} · {answer.latency_ms} ms")
    )
    table = Table("#", "cited", "kind", "source", "ref", "snippet")
    for s in answer.sources:
        table.add_row(
            str(s.n),
            "✓" if s.cited else "",
            s.kind,
            escape(source_label(s.path, s.page, s.ref)),
            escape(s.ref),
            escape(s.snippet[:80]),
        )
    common.console.print(table)


def print_wiki_report(report: WikiReport) -> None:
    """Сводка `wiki build`: сколько страниц создано, обновлено, удалено и не удалось."""
    common.console.print(
        f"wiki: created {len(report.created)}, updated {len(report.updated)}, "
        f"deleted {len(report.deleted)}, skipped {len(report.skipped)}, "
        f"failed {len(report.failed)}"
    )
    for name in report.failed:
        common.console.print(f"[red]✗[/red] wiki page {escape(name)} (retried on the next build)")


def check_status(check: Check) -> str:
    """Цветной статус проверки `doctor`: fail, warn или ok."""
    if not check.ok:
        return "[red]fail[/red]"
    return "[yellow]warn[/yellow]" if check.warn else "[green]ok[/green]"


class _Count(ProgressColumn):
    """Колонка `done/total`; пусто, пока total неизвестен (parse, chunk, embed)."""

    def render(self, task: Task) -> Text:
        if task.total is None:
            return Text("")
        return Text(f"{int(task.completed)}/{int(task.total)}", style="progress.download")


class RichProgress:
    """Реализация `ProgressSink` поверх rich.progress: один бар для файлов запуска, другой —
    для текущего шага (стадии текущего файла или шага wiki / reindex)."""

    def __init__(self, bars: Progress):
        self.bars = bars
        self._files: TaskID | None = None
        self._step: TaskID | None = None
        self._file = ""  # короткое имя файла, который сейчас обрабатывается
        self._failed = 0  # упавшие единицы текущего шага
        self._outcomes: Counter[str] = Counter()  # итоги по файлам: processed, skipped, failed

    def files_found(self, total: int) -> None:
        self._files = self.bars.add_task("files", total=total, note="")

    def file_started(self, path: str) -> None:
        name = Path(path).name
        self._file = name if len(name) <= 40 else f"{name[:39]}…"

    def file_done(self, status: str) -> None:
        self._outcomes[status] += 1
        note = " · ".join(f"{n} {s}" for s, n in sorted(self._outcomes.items()))
        if self._files is not None:
            self.bars.update(self._files, advance=1, note=note)
        self._file = ""
        self._drop_step()

    def step(self, name: str, total: int | None = None) -> None:
        self._drop_step()  # новый шаг: бар, счётчик и время начинаются заново
        label = f"{self._file} · {name}" if self._file else name
        self._step = self.bars.add_task(escape(label), total=total, note="")
        self._failed = 0

    def advance(self, n: int = 1, *, failed: bool = False) -> None:
        if self._step is None:
            return
        self._failed += n if failed else 0
        note = f"[red]{self._failed} failed[/red]" if self._failed else ""
        self.bars.update(self._step, advance=n, note=note)

    def _drop_step(self) -> None:
        if self._step is not None:
            self.bars.remove_task(self._step)
            self._step = None


@contextmanager
def progress() -> Iterator[RichProgress]:
    """Живой прогресс на время долгой команды. Бары исчезают по окончании (transient), а
    вместо них команда печатает сводку. Вне терминала (pipe, CI) ничего не рисуется."""
    bars = Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        _Count(),
        TimeElapsedColumn(),
        TextColumn("{task.fields[note]}"),
        console=common.console,
        transient=True,
        disable=not common.console.is_terminal,
    )
    with bars:
        sink = RichProgress(bars)
        sink.step("starting")
        yield sink
