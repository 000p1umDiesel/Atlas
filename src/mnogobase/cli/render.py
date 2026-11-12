"""Вывод команд в терминал: живой прогресс долгих операций, ответы со ссылками на источники
и сводки построения wiki.

Весь текст документов и ответов LLM печатается через `escape(...)`: квадратные скобки в нём
(например, ссылки `[1]`) не должны разбираться как разметка Rich, а документ не должен
подменять оформление вывода своей разметкой.

Всё печатается в `common.console`, чтобы тесты могли перехватить вывод в одном месте.
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
    return f"{where}, стр. {page}" if page else where


def print_answer(answer: Answer) -> None:
    """Ответ в рамке (с режимом и задержкой в заголовке) и таблица источников.

    Галочкой в колонке «цит.» отмечены источники, на которые ответ действительно ссылается;
    остальные попали в контекст, но в ответе не использованы. Сниппет обрезан до 80 символов."""
    common.console.print(
        Panel(escape(answer.text), title=f"{answer.mode} · {answer.latency_ms} мс")
    )
    table = Table("#", "цит.", "тип", "источник", "ref", "фрагмент")
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
        f"wiki: создано {len(report.created)}, обновлено {len(report.updated)}, "
        f"удалено {len(report.deleted)}, пропущено {len(report.skipped)}, "
        f"с ошибкой {len(report.failed)}"
    )
    for name in report.failed:
        common.console.print(
            f"[red]✗[/red] страница wiki {escape(name)} (повторится при следующей сборке)"
        )


def check_status(check: Check) -> str:
    """Цветной статус проверки `doctor`: fail, warn или ok."""
    if not check.ok:
        return "[red]fail[/red]"
    return "[yellow]warn[/yellow]" if check.warn else "[green]ok[/green]"


class _Count(ProgressColumn):
    """Колонка `done/total`; пусто, пока total неизвестен (parse, chunk, embed)."""

    def render(self, task: Task) -> Text:
        """Текст колонки для одной задачи rich.progress."""
        if task.total is None:
            return Text("")
        return Text(f"{int(task.completed)}/{int(task.total)}", style="progress.download")


class RichProgress:
    """Реализация `ProgressSink` поверх rich.progress: один бар для файлов запуска, другой —
    для текущего шага (стадии текущего файла или шага wiki / reindex).

    Пайплайн сообщает о ходе работы вызовами `files_found` → (`file_started` → `step` →
    `advance`... → `file_done`) для каждого файла; шаги без файла (wiki, reindex) приходят
    просто как `step` / `advance`. Бар шага пересоздаётся на каждом шаге, чтобы счётчик и
    время показывали только текущую стадию."""

    def __init__(self, bars: Progress):
        self.bars = bars
        self._files: TaskID | None = None
        self._step: TaskID | None = None
        self._file = ""  # короткое имя файла, который сейчас обрабатывается
        self._failed = 0  # упавшие единицы текущего шага
        self._outcomes: Counter[str] = Counter()  # итоги по файлам: processed, skipped, failed

    def files_found(self, total: int) -> None:
        """Создаёт бар файлов: всего `total` файлов в запуске."""
        self._files = self.bars.add_task("файлы", total=total, note="")

    def file_started(self, path: str) -> None:
        """Запоминает имя текущего файла (до 40 символов) для подписи бара шага."""
        name = Path(path).name
        self._file = name if len(name) <= 40 else f"{name[:39]}…"

    def file_done(self, status: str) -> None:
        """Файл обработан с итогом `status`: сдвигает бар файлов, обновляет сводку итогов
        и убирает бар шага."""
        self._outcomes[status] += 1
        note = " · ".join(f"{n} {s}" for s, n in sorted(self._outcomes.items()))
        if self._files is not None:
            self.bars.update(self._files, advance=1, note=note)
        self._file = ""
        self._drop_step()

    def step(self, name: str, total: int | None = None) -> None:
        """Начинает шаг `name`; `total` — число единиц шага, если оно известно заранее."""
        self._drop_step()  # новый шаг: бар, счётчик и время начинаются заново
        label = f"{self._file} · {name}" if self._file else name
        self._step = self.bars.add_task(escape(label), total=total, note="")
        self._failed = 0

    def advance(self, n: int = 1, *, failed: bool = False) -> None:
        """Сдвигает бар шага на `n` единиц; `failed=True` считает их неудавшимися и
        показывает красный счётчик ошибок."""
        if self._step is None:
            return
        self._failed += n if failed else 0
        note = f"[red]ошибок: {self._failed}[/red]" if self._failed else ""
        self.bars.update(self._step, advance=n, note=note)

    def _drop_step(self) -> None:
        """Убирает бар текущего шага, если он есть."""
        if self._step is not None:
            self.bars.remove_task(self._step)
            self._step = None


@contextmanager
def progress() -> Iterator[RichProgress]:
    """Живой прогресс на время долгой команды. Бары исчезают по окончании (transient), а
    вместо них команда печатает сводку. Вне терминала (pipe, CI) ничего не рисуется.

    Сразу показывается шаг «подготовка», чтобы до первого сообщения пайплайна (подготовка
    индекса, первый запрос к модели) пользователь видел, что команда работает."""
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
        sink.step("подготовка")
        yield sink
