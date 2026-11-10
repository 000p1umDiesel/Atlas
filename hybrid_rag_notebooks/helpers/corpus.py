"""Документы и вопросы: поиск файлов, скачивание статьи, чтение questions.jsonl."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx

EXTENSIONS = {".pdf", ".docx", ".pptx", ".html", ".md", ".png", ".jpg", ".jpeg", ".tiff"}


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def ensure_corpus(root: Path) -> Path:
    """Скачивает статью с arXiv, если её нет, и сверяет SHA256 с data/questions.meta.json.

    Эталонные ответы написаны по конкретной версии PDF (v7), поэтому другой файл
    под тем же именем не подойдёт.
    """
    meta = json.loads((root / "data" / "questions.meta.json").read_text(encoding="utf-8"))
    path = root / "data" / meta["source_file"]
    if not path.exists():
        response = httpx.get(meta["source_download_url"], follow_redirects=True, timeout=120)
        response.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(response.content)
    if sha256(path) != meta["source_sha256"]:
        raise ValueError(f"{path.name}: SHA256 не совпадает с версией {meta['source_version']}")
    return path


def discover_documents(folder: Path) -> list[Path]:
    paths = sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in EXTENSIONS)
    if not paths:
        raise FileNotFoundError(f"В {folder} нет документов с расширениями {sorted(EXTENSIONS)}")
    return paths


def document_inventory(paths: list[Path], folder: Path) -> list[dict]:
    """Список документов с хешами: он попадает в настройки индекса."""
    return [{"path": p.relative_to(folder).as_posix(), "sha256": sha256(p)} for p in paths]


def load_questions(path: Path) -> list[dict]:
    questions, seen = [], set()
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not str(row.get("id", "")).strip() or not str(row.get("question", "")).strip():
            raise ValueError(f"{path.name}, строка {number}: нужны непустые id и question")
        if row["id"] in seen:
            raise ValueError(f"{path.name}, строка {number}: повторяется id {row['id']}")
        seen.add(row["id"])
        row["question"] = row["question"].strip()
        questions.append(row)
    if not questions:
        raise ValueError(f"{path.name}: вопросов нет")
    return questions
