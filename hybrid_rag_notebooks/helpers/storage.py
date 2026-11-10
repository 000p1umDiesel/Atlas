"""Файлы результатов: JSON с настройками и JSONL, который дописывается по одной строке.

Благодаря JSONL долгие ячейки можно прервать и запустить снова: готовые строки
читаются с диска, считается только то, чего ещё нет.
"""

from __future__ import annotations

import json
from pathlib import Path


def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def check_config(path: Path, config: dict) -> None:
    """Первый запуск записывает настройки, следующие сверяют их с записанными.

    Это защита от смешивания: если поменяли модель или параметры, а имя индекса
    или запуска оставили прежним, старые и новые результаты легли бы в одну папку.
    """
    path = Path(path)
    if not path.exists():
        write_json(path, config)
        return
    saved = read_json(path)
    if saved != json.loads(json.dumps(config, ensure_ascii=False)):
        changed = sorted(k for k in {*saved, *config} if saved.get(k) != config.get(k))
        raise ValueError(
            f"{path.parent.name}: настройки отличаются от сохранённых ({', '.join(changed)}). "
            "Задайте новое имя или удалите старую папку."
        )


def append_jsonl(path: Path, row: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    lines = path.read_text(encoding="utf-8").splitlines()
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            if number == len(lines):
                break  # строка оборвалась при прерывании, её посчитаем заново
            raise
    return rows


def latest_records(path: Path, key=("id",)) -> dict:
    """Последняя запись для каждого ключа: повторная попытка заменяет предыдущую."""
    result = {}
    for row in read_jsonl(path):
        value = tuple(row[k] for k in key)
        result[value[0] if len(key) == 1 else value] = row
    return result
