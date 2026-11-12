"""Манифест эксперимента, последние записи JSONL и набор вопросов для оценки."""

from __future__ import annotations

import json
from pathlib import Path

from mnogobase.core.files import fingerprint, read_jsonl, write_json


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


def latest_records(path: Path, keys: tuple[str, ...] = ("id",)) -> dict:
    result = {}
    for number, row in enumerate(read_jsonl(path, recover_tail=True), 1):
        missing = [k for k in keys if k not in row]
        if missing:
            raise ValueError(f"{path}, record {number}: no {', '.join(missing)}")
        key = tuple(row[k] for k in keys)
        result[key[0] if len(key) == 1 else key] = row
    return result


def load_questions(path: Path) -> list[dict]:
    if not path.is_file():
        raise FileNotFoundError(path)
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
