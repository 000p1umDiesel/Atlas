"""Отрисовка `mnogobase models up` в терминале: живая таблица в TTY, строки — без него
(nohup, cron, CI: лог-файл не забивается перерисовками)."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager

from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape
from rich.progress_bar import ProgressBar
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from mnogobase.model_watch import RoleStatus

PLAIN_EVERY = 30.0  # без TTY: строка о скачивании не чаще, чем раз в столько секунд
_PHASE_TEXT = {
    "queued": "[dim]waiting to start[/dim]",
    "starting": "starting (download size unknown)",
    "downloading": "downloading",
    "loading": "loading into memory",
    "ready": "[green]✓ ready[/green]",
}


def human_bytes(n: float) -> str:
    for unit, scale in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= scale:
            value = n / scale
            return (
                f"{value:.0f} {unit}"
                if value >= 100 or unit != "GB" and value >= 10
                else f"{value:.1f} {unit}"
            )
    return f"{int(n)} B"


def human_time(seconds: float) -> str:
    seconds = int(seconds)
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{sec:02d}" if hours else f"{minutes}:{sec:02d}"


def weights_label(status: RoleStatus) -> str:
    """`Qwen3-Embedding-4B:Q4_K_M` вместо `Qwen/Qwen3-Embedding-4B-GGUF:Q4_K_M`: организация
    и суффикс репозитория есть в config."""
    return status.served.settings.weights.rpartition("/")[2].replace("-GGUF:", ":")


def _pair(done: float, total: float) -> str:
    """`1.3/2.5 GB`: обе величины в единицах `total`."""
    unit, scale = next(((u, k) for u, k in (("GB", 1e9), ("MB", 1e6)) if total >= k), ("KB", 1e3))
    return f"{done / scale:.1f}/{total / scale:.1f} {unit}"


WIDE = 110  # от этой ширины терминала показывается колонка backend
_DETAILS_WIDTH = len("1.3/2.5 GB · 38 MB/s · 0:31 left")


def _state(status: RoleStatus) -> Spinner | ProgressBar | Text:
    """Полоса при скачивании, иначе спиннер или итог."""
    if status.phase == "downloading" and status.total:
        return ProgressBar(total=status.total, completed=status.done, width=18)
    text = Text.from_markup(_PHASE_TEXT[status.phase])
    if status.phase in ("starting", "downloading", "loading"):
        return Spinner("dots", text=text)
    return text


def _details(status: RoleStatus) -> str:
    if status.phase == "downloading" and status.total:
        parts = [_pair(status.done, status.total)]
        if status.rate:
            parts.append(f"{human_bytes(status.rate)}/s")
        if status.eta is not None:
            parts.append(f"{human_time(status.eta)} left")
        return " · ".join(parts)
    if status.phase in ("loading", "ready") and status.total:
        return "cached" if status.cached else f"{human_bytes(status.total)} downloaded"
    return ""


def up_table(statuses: Sequence[RoleStatus], elapsed: float, width: int = WIDE) -> Group:
    wide = width >= WIDE
    table = Table(box=None, pad_edge=False, show_edge=False, header_style="bold")
    # при нехватке места сокращается только имя модели
    table.add_column("role", no_wrap=True, min_width=8)
    if wide:
        table.add_column("backend", no_wrap=True)
    # rich при нехватке места урезает все колонки; ширина имени — что осталось от остальных
    fixed = 8 + 21 + _DETAILS_WIDTH + (8 if wide else 0) + 2 * (4 if wide else 3)
    table.add_column("weights", overflow="ellipsis", no_wrap=True, max_width=max(12, width - fixed))
    table.add_column("state", no_wrap=True, min_width=21)
    table.add_column("", no_wrap=True)  # байты, скорость, ETA
    for status in statuses:
        cells = [status.served.role]
        if wide:
            cells.append(status.served.backend)
        cells += [escape(weights_label(status)), _state(status), _details(status)]
        table.add_row(*cells)
    footer = Text(f"elapsed {human_time(elapsed)}", style="dim")
    return Group(table, footer)


class UpReporter:
    """Получает свежие состояния ролей на каждом опросе `models up`."""

    def __init__(self, console: Console, clock: Callable[[], float] = time.monotonic):
        self._console = console
        self._clock = clock
        self._start = clock()
        self._live: Live | None = None
        self._phases: dict[str, str] = {}
        self._printed: dict[str, float] = {}

    def update(self, statuses: Sequence[RoleStatus]) -> None:
        elapsed = self._clock() - self._start
        if self._live is not None:
            self._live.update(up_table(statuses, elapsed, self._console.width))
            return
        for status in statuses:
            self._plain(status, elapsed)

    def _plain(self, status: RoleStatus, elapsed: float) -> None:
        role = status.served.role
        now = self._clock()
        changed = self._phases.get(role) != status.phase
        due = now - self._printed.get(role, -PLAIN_EVERY) >= PLAIN_EVERY
        if not changed and not (status.phase == "downloading" and due):
            return
        self._phases[role] = status.phase
        self._printed[role] = now
        state = Text.from_markup(_PHASE_TEXT[status.phase]).plain
        details = _details(status)
        line = f"[{human_time(elapsed)}] {role}: {state}" + (f" — {details}" if details else "")
        self._console.print(line, soft_wrap=True, highlight=False, markup=False)

    @contextmanager
    def live(self) -> Iterator[UpReporter]:
        if not self._console.is_terminal:
            yield self
            return
        with Live(console=self._console, refresh_per_second=4, transient=False) as live:
            self._live = live
            try:
                yield self
            finally:
                self._live = None
