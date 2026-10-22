"""`mnogobase logs`: читаемый вывод `logs/mnogobase.jsonl` — хвост файла и слежение (`-f`).

Каждая строка файла — JSON-событие structlog (`event`, `level`, `ts`, `run_id`, `doc_id` и
поля события). Частые события (вызов LLM, этап, слияние сущностей, итоги) показываются
коротко, остальные — как `ключ=значение`."""

from __future__ import annotations

import json
import os
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rich.text import Text

LEVELS = ("debug", "info", "warning", "error", "critical")
_LEVEL_STYLE = {"warning": "yellow", "error": "bold red", "critical": "bold red"}
_LEVEL_LABEL = {"warning": "WARN", "critical": "CRIT"}
_HIDDEN = {"event", "level", "ts", "run_id", "doc_id", "logger", "exception"}


@dataclass(frozen=True)
class LogFilter:
    level: str = "info"
    run_id: str | None = None
    event: str | None = None  # подстрока имени события
    http: bool = False  # строки httpx "HTTP Request: ..." дублируют llm_call

    def accepts(self, record: dict) -> bool:
        level = str(record.get("level", "info")).lower()
        if level in LEVELS and LEVELS.index(level) < LEVELS.index(self.level):
            return False
        if self.run_id and not str(record.get("run_id", "")).startswith(self.run_id):
            return False
        event = str(record.get("event", ""))
        if not self.http and event.startswith("HTTP Request:"):
            return False
        return not self.event or self.event in event


def parse_line(line: str) -> dict | None:
    try:
        record = json.loads(line)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def last_run_id(path: Path) -> str | None:
    """`run_id` последнего запуска (ingest, wiki build) в файле."""
    run_id = None
    for record in _records(_lines(path)):
        run_id = record.get("run_id") or run_id
    return run_id


def _lines(path: Path) -> Iterator[str]:
    with path.open(encoding="utf-8", errors="replace") as file:
        yield from file


def _records(lines: Iterable[str]) -> Iterator[dict]:
    for line in lines:
        record = parse_line(line)
        if record is not None:
            yield record


def tail(path: Path, n: int, flt: LogFilter) -> list[dict]:
    """Последние `n` событий, прошедших фильтр."""
    if not path.exists():
        return []
    return list(deque((r for r in _records(_lines(path)) if flt.accepts(r)), maxlen=n))


def follow(
    path: Path,
    flt: LogFilter,
    *,
    interval: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    stop: Callable[[], bool] = lambda: False,
) -> Iterator[dict]:
    """Новые события по мере записи (как `tail -f`). Переживает ротацию: RotatingFileHandler
    переименовывает файл и начинает новый — тогда файл открывается заново с начала."""
    # файл, который уже есть, читается с конца: его историю показал `tail`
    skip_existing = path.exists()
    file = None
    inode = None
    buffer = ""
    try:
        while not stop():
            if file is None and path.exists():
                file = path.open(encoding="utf-8", errors="replace")
                inode = os.fstat(file.fileno()).st_ino
                if skip_existing:
                    file.seek(0, os.SEEK_END)
                skip_existing = False
            if file is not None:
                chunk = file.read()
                if chunk:
                    buffer += chunk
                    *lines, buffer = buffer.split("\n")
                    yield from (r for r in _records(lines) if flt.accepts(r))
                    continue
                if _rotated(path, inode, file.tell()):
                    file.close()
                    file, buffer = None, ""
                    continue
            sleep(interval)
    finally:
        if file is not None:
            file.close()


def _rotated(path: Path, inode: int | None, position: int) -> bool:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return True
    return stat.st_ino != inode or stat.st_size < position


def _seconds(ms: object) -> str:
    return f"{int(ms) / 1000:.1f} s" if isinstance(ms, int | float) else "?"


def _short(value: object, limit: int = 60) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _summary(record: dict) -> str:
    """Короткая строка для частых событий; `None`-подобные поля опускаются."""
    event = record.get("event")
    r = record
    if event == "llm_call":
        retries = f" · {r['retries']} retries" if r.get("retries") else ""
        return (
            f"{r.get('task')} · {r.get('model')} · {r.get('tokens_in')}→{r.get('tokens_out')} tok"
            f" · {_seconds(r.get('duration_ms'))}{retries}"
        )
    if event == "stage_done":
        return f"{r.get('stage')} · {_seconds(r.get('duration_ms'))} · {Path(str(r.get('path', ''))).name}"
    if event == "stage_failed":
        return (
            f"{r.get('stage')} · {Path(str(r.get('path', ''))).name} · "
            f"{r.get('error_type')}: {_short(r.get('error'), 120)}"
        )
    if event == "entity_merged":
        return f"{r.get('name')} → {r.get('into')} ({r.get('method')}, {r.get('score')})"
    if event == "parsed":
        return f"{r.get('title')} · {r.get('n_pages')} pages · {r.get('n_images')} images"
    fields = {k: v for k, v in r.items() if k not in _HIDDEN}
    return " ".join(f"{k}={_short(v)}" for k, v in fields.items())


def _local(ts: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def needs_dates(records: Iterable[dict]) -> bool:
    """Дата нужна, если среди событий есть не сегодняшние."""
    today = datetime.now().astimezone().date()
    return any((m := _local(r.get("ts"))) is not None and m.date() != today for r in records)


def render(record: dict, *, show_date: bool = False) -> Text:
    """`12:04:31 INFO  llm_call  extract · gpt · 1062→737 tok · 7.0 s  run 2659c0 doc bdfaa6`."""
    level = str(record.get("level", "info")).lower()
    style = _LEVEL_STYLE.get(level, "")
    moment = _local(record.get("ts"))
    time_text = moment.strftime("%m-%d %H:%M:%S" if show_date else "%H:%M:%S") if moment else "?"
    line = Text(time_text + " ", style="dim")
    line.append(f"{_LEVEL_LABEL.get(level, level.upper()):<5} ", style=style or "dim")
    event = str(record.get("event", ""))
    line.append(f"{_short(event, 40):<22}", style=style or "cyan")
    line.append(" " + _summary(record))
    context = []
    if record.get("run_id"):
        context.append(f"run {str(record['run_id'])[:6]}")
    if record.get("doc_id"):
        context.append(f"doc {str(record['doc_id'])[:6]}")
    if context:
        line.append("  " + " ".join(context), style="dim")
    exception = record.get("exception")
    if exception:
        line.append("\n" + _exception_text(exception), style="red dim")
    return line


def _exception_text(exception: object) -> str:
    """Последний кадр и сообщение структурного traceback structlog (полный — в файле)."""
    if isinstance(exception, list) and exception:
        last = exception[-1] if isinstance(exception[-1], dict) else {}
        frames = last.get("frames") or []
        where = ""
        if frames and isinstance(frames[-1], dict):
            frame = frames[-1]
            where = f" at {frame.get('filename')}:{frame.get('lineno')}"
        return f"    {last.get('exc_type')}: {_short(last.get('exc_value'), 200)}{where}"
    return "    " + _short(exception, 300)
