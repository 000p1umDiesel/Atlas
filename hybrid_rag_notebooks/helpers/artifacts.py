"""Сохранение прогресса в JSONL; один ноутбук записывает один каталог результатов."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def fingerprint(value) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return hashlib.sha256(body.encode()).hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    tmp.replace(path)


def ensure_manifest(path: Path, config: dict) -> dict:
    """Проверяем, что сохранённый запуск использует тот же корпус, промпт, модель и вопросы."""
    expected = {"fingerprint": fingerprint(config), "config": config}
    if path.exists():
        actual = json.loads(path.read_text(encoding="utf-8"))
        if actual != expected:
            raise ValueError(
                f"{path}: configuration changed; choose a new output directory/index name"
            )
    else:
        write_json(path, expected)
    return expected


def read_jsonl(path: Path, *, recover_tail: bool = False) -> list[dict]:
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
                raise ValueError("expected an object")
        except (ValueError, TypeError) as exc:
            if recover_tail and number == len(lines) and not body.endswith(b"\n"):
                break  # оборванную строку исправит append_jsonl перед следующей записью
            raise ValueError(f"{path}, line {number}: {exc}") from exc
        rows.append(row)
    return rows


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    has_tail = False
    if path.exists() and path.stat().st_size:
        with path.open("rb") as stream:
            stream.seek(-1, os.SEEK_END)
            has_tail = stream.read(1) != b"\n"
    if has_tail:
        # Исправляем только оборванную последнюю строку; остальные ошибки требуют проверки.
        valid = read_jsonl(path, recover_tail=True)
        tmp = path.with_suffix(".repair")
        tmp.write_text(
            "".join(json.dumps(r, ensure_ascii=False, allow_nan=False) + "\n" for r in valid),
            encoding="utf-8",
        )
        tmp.replace(path)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def latest_records(path: Path, keys: tuple[str, ...] = ("id",)) -> dict:
    result = {}
    for row in read_jsonl(path, recover_tail=True):
        key = tuple(row[k] for k in keys)
        result[key[0] if len(key) == 1 else key] = row
    return result


def load_questions(path: Path) -> list[dict]:
    rows = read_jsonl(path)
    seen = set()
    for number, row in enumerate(rows, 1):
        if any(not isinstance(row.get(k), str) or not row[k].strip() for k in ("id", "question")):
            raise ValueError(f"{path}, line {number}: nonempty string id and question required")
        # Поиск также удаляет крайние пробелы: манифест и ответ должны хранить один вопрос.
        row["question"] = row["question"].strip()
        if row["id"] in seen:
            raise ValueError(f"{path}, line {number}: duplicate id {row['id']}")
        if (
            "reference" in row
            and row["reference"] is not None
            and not isinstance(row["reference"], str)
        ):
            raise ValueError(f"{path}, line {number}: reference must be a string or null")
        seen.add(row["id"])
    if not rows:
        raise ValueError(f"{path}: dataset is empty")
    return rows
