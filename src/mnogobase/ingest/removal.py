"""Select registered sources without traversing or modifying the source filesystem."""

from __future__ import annotations

import stat
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from mnogobase.stores.registry import FileRow, Registry
from mnogobase.wiki.builder import WikiReport


@dataclass
class RemovalReport:
    removed: list[str] = field(default_factory=list)
    retained: list[str] = field(default_factory=list)
    wiki: WikiReport | None = None


def select_removals(
    registry: Registry, paths: Sequence[Path], *, missing: bool = False
) -> list[FileRow]:
    if not paths and not missing:
        raise ValueError("Provide source paths or --missing")
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
