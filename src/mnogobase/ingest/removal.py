"""Выбор источников для `mnogobase remove` по registry, без обхода и изменения файлов.

`select_removals` вызывают CLI-команда `remove` (чтобы показать выбор до подтверждения) и
`Pipeline.remove` (чтобы удалить). Выбор идёт по строкам `files`, а не по диску, поэтому
можно забыть уже удалённый файл или целый каталог; исходные файлы никогда не трогаются.
"""

from __future__ import annotations

import stat
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from mnogobase.stores.registry import FileRow, Registry
from mnogobase.wiki.builder import WikiReport


@dataclass
class RemovalReport:
    """Итог `Pipeline.remove`.

    `removed` — забытые пути; `retained` — `doc_id`, знание которых осталось, потому что то
    же содержимое лежит по другому, не выбранному пути; `wiki` — отчёт сборки wiki."""

    removed: list[str] = field(default_factory=list)
    retained: list[str] = field(default_factory=list)
    wiki: WikiReport | None = None


def select_removals(
    registry: Registry, paths: Sequence[Path], *, missing: bool = False
) -> list[FileRow]:
    """Строки `files`, которые нужно забыть.

    `paths` — файлы или каталоги (абсолютизируются; каталог выбирает всё под ним). С
    `missing=True` из выбранного остаются только строки, по пути которых больше нет
    обычного файла; без `paths` — среди всех строк. Только чтение registry и `stat`
    источников. Ни путей, ни `missing` — `ValueError`."""
    if not paths and not missing:
        raise ValueError("укажите пути к источникам или --missing")
    targets = [Path(p).resolve() for p in paths]
    selected = []
    for row in registry.files():
        source = Path(row.path)
        if targets and not any(source == p or source.is_relative_to(p) for p in targets):
            continue
        if missing:
            try:
                if stat.S_ISREG(source.stat().st_mode):
                    continue
            except (FileNotFoundError, NotADirectoryError):
                pass
        selected.append(row)
    return selected
