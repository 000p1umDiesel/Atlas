"""Файловые записи, которые не оставляют обрезанных файлов и не растут бесконечно."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4


def write_atomic(path: Path, text: str) -> None:
    """Пишет текст во временный файл рядом и переименовывает его: читатель видит либо
    старое, либо новое содержимое целиком, но не обрезанное после сбоя."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def rotate_if_large(path: Path, max_bytes: int) -> None:
    """Журнал, который только дописывается: больше `max_bytes` — переименовывается в
    `<stem>.1<suffix>` (прежняя копия заменяется), запись продолжается в новый файл."""
    try:
        if path.stat().st_size <= max_bytes:
            return
    except FileNotFoundError:
        return
    path.replace(path.with_name(f"{path.stem}.1{path.suffix}"))
