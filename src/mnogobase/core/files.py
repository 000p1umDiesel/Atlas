"""Файловые записи, которые не оставляют обрезанных файлов и не растут бесконечно,
и устойчивый к обрыву JSONL.

Через этот модуль пишутся производные файлы проекта: кэш чанков, страницы wiki и
настройки Obsidian, маркер проекта, артефакты eval, журналы compare и processing.
`write_atomic` даёт атомарную замену файла целиком, `append_jsonl` / `read_jsonl` —
журналы, которые переживают падение процесса посреди записи строки, `fingerprint` —
стабильный хэш JSON-значения для манифестов и версий.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4


def write_atomic(path: Path, text: str) -> None:
    """Пишет текст во временный файл рядом и переименовывает его: читатель видит либо
    старое, либо новое содержимое целиком, но не обрезанное после сбоя.

    Создаёт родительские каталоги. Временный файл лежит в том же каталоге, потому что
    атомарное переименование возможно только в пределах одной файловой системы; уникальное
    имя не даёт параллельным писателям затереть чужой временный файл. При ошибке временный
    файл удаляется. fsync не делается: защита от падения процесса, а не питания."""
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


def fingerprint(value) -> str:
    """sha256 (hex) канонического JSON значения: ключи отсортированы, поэтому порядок в
    словаре не влияет на результат.

    Raises:
        TypeError: значение не сериализуется в JSON.
        ValueError: в значении есть NaN или бесконечность.
    """
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return hashlib.sha256(body.encode()).hexdigest()


def write_json(path: Path, value) -> None:
    """Атомарно записывает `value` как отформатированный JSON."""
    write_atomic(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def read_jsonl(path: Path, *, recover_tail: bool = False) -> list[dict]:
    """Читает JSONL-файл как список объектов; нет файла — пустой список, пустые строки
    пропускаются.

    `recover_tail=True` молча отбрасывает последнюю строку, если она не разбирается и после
    неё нет перевода строки, — это след записи, оборванной падением процесса. Любая другая
    испорченная строка — повод для ручной проверки, а не для молчаливой потери данных.

    Raises:
        ValueError: строка не JSON-объект (с путём и номером строки в сообщении).
    """
    if not path.exists():
        return []
    body = path.read_bytes()
    lines = body.splitlines()
    rows = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line.decode("utf-8"))
            if not isinstance(row, dict):
                raise ValueError("ожидался JSON-объект")
        except (ValueError, TypeError) as exc:
            if recover_tail and number == len(lines) and not body.endswith(b"\n"):
                break  # оборванную строку исправит append_jsonl перед следующей записью
            raise ValueError(f"{path}, строка {number}: {exc}") from exc
        rows.append(row)
    return rows


def append_jsonl(path: Path, row: dict) -> None:
    """Дописывает объект строкой в JSONL-файл и делает fsync.

    Если файл заканчивается не переводом строки (прошлая запись оборвалась), он сначала
    атомарно переписывается без оборванного хвоста, чтобы новая строка не склеилась с
    мусором. fsync нужен, потому что журнал служит точкой возобновления (eval, processing).

    Raises:
        ValueError: в файле испорчена не последняя строка (см. `read_jsonl`).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    has_tail = False
    if path.exists() and path.stat().st_size:
        with path.open("rb") as stream:
            stream.seek(-1, os.SEEK_END)
            has_tail = stream.read(1) != b"\n"
    if has_tail:
        # Исправляем только оборванную последнюю строку; остальные ошибки требуют проверки.
        valid = read_jsonl(path, recover_tail=True)
        write_atomic(
            path,
            "".join(json.dumps(r, ensure_ascii=False, allow_nan=False) + "\n" for r in valid),
        )
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
