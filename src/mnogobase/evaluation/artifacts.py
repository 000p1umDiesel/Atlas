"""Манифест эксперимента, последние записи JSONL и набор вопросов для оценки.

Манифест (`run.json`, `judge.json`) — JSON вида `{"fingerprint": sha256, "config": {...}}`.
Он фиксирует условия эксперимента: данные в каталоге имеют смысл только при тех же условиях,
поэтому несовпадение — ошибка, а не повод молча перезаписать.
"""

from __future__ import annotations

import json
from pathlib import Path

from mnogobase.core.files import fingerprint, read_jsonl, write_json


def ensure_manifest(path: Path, config: dict) -> dict:
    """Создаёт манифест или проверяет, что сохранённый совпадает с `config`.

    Если файла нет, атомарно записывает `{"fingerprint", "config"}` и возвращает его.
    Если есть, требует точного совпадения с ожидаемым (тот же корпус, промпт, модель,
    вопросы и т. д.), иначе `ValueError`: продолжать эксперимент в этом каталоге с другой
    конфигурацией нельзя, нужен новый каталог."""
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
    """Последняя запись JSONL-файла для каждого ключа.

    Ключ словаря — значение единственного поля из `keys` или кортеж значений, если полей
    несколько. Более поздняя запись с тем же ключом заменяет раннюю: так повтор после
    ошибки «перекрывает» неудачную попытку, не трогая файл. Оборванная последняя строка
    пропускается. Отсутствующий файл — пустой словарь; запись без ключевого поля —
    `ValueError`."""
    result = {}
    for number, row in enumerate(read_jsonl(path, recover_tail=True), 1):
        missing = [k for k in keys if k not in row]
        if missing:
            raise ValueError(f"{path}, record {number}: no {', '.join(missing)}")
        key = tuple(row[k] for k in keys)
        result[key[0] if len(key) == 1 else key] = row
    return result


def load_questions(path: Path) -> list[dict]:
    """Читает и проверяет набор вопросов (JSONL).

    У каждой строки обязательны непустые строковые `id` (уникальный) и `question`;
    `reference` — строка или null. Вопрос обрезается от крайних пробелов. Остальные поля
    (например, `category`) сохраняются как есть. Пустой набор, повтор `id` или неверный тип —
    `ValueError` с номером строки; нет файла — `FileNotFoundError`."""
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
