"""Детерминированные идентификаторы: одинаковый вход всегда даёт одинаковый id.

На этом держится идемпотентность всего ingest: повторная обработка документа
перезаписывает те же точки Qdrant и узлы Neo4j (upsert / `MERGE`) вместо создания дублей.
- `doc_id` — первые 16 hex-символов sha256 байтов файла: тот же файл под другим путём —
  тот же документ, изменённый файл — новый;
- `chunk_id` = `<doc_id>:<номер чанка из 5 цифр>`;
- `entity_id` — хэш типа и нормализованного имени (`normalize_name`), поэтому «C++» и
  «c++» — одна сущность, а одно имя под разными типами — разные; `page_id` wiki равен
  `entity_id`;
- id точки Qdrant — `uuid5` от строкового ключа (`point_id`): Qdrant принимает только
  числа и UUID.

Менять любую из этих функций нельзя без полной переиндексации: старые id перестанут
совпадать с новыми.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from pathlib import Path

# пространство имён uuid5 для точек Qdrant; смена = новые id у всех точек
NAMESPACE = uuid.UUID("5b0c9a8e-3f61-4d2a-9b7e-0c1d2e3f4a5b")
# сохраняем + и #, чтобы "C++" / "C#" не схлопывались в "c"
_SEPARATORS = re.compile(r"[^\w+#]+|_+", re.UNICODE)
# "<slug>.md" должен быть заметно короче типичного лимита имени файла в 255 байт
_SLUG_MAX_CHARS = 80
_SLUG_MAX_BYTES = 200


def file_doc_id(path: Path) -> str:
    """`doc_id` файла: первые 16 hex-символов sha256 его байтов (читается блоками по 1 МиБ,
    чтобы не грузить большой файл в память).

    Raises:
        OSError: файл не читается.
    """
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()[:16]


def chunk_id(doc_id: str, idx: int) -> str:
    """`<doc_id>:<idx>` с номером из 5 цифр: строковая сортировка совпадает с порядком
    чанков в документе."""
    return f"{doc_id}:{idx:05d}"


def normalize_name(name: str) -> str:
    """Нормализованное имя для сравнения: NFKC, casefold, разделители (кроме `+` и `#`) и
    подчёркивания заменены пробелом, пробелы схлопнуты.

    Например, "Machine-Learning" и "machine learning" дают одно и то же. Входит в
    `entity_id` и `slugify`."""
    text = unicodedata.normalize("NFKC", name).casefold()
    return " ".join(_SEPARATORS.sub(" ", text).split())


def name_key(name: str) -> str:
    """Ключ для поиска кандидатов на слияние: без пробелов и дефисов ("MagAO-X" = "MagAOX")."""
    return normalize_name(name).replace(" ", "")


def entity_name_keys(name: str, aliases: list[str]) -> list[str]:
    """Отсортированные уникальные `name_key` имени и всех псевдонимов, без пустых;
    хранятся у сущности для поиска кандидатов на слияние."""
    return sorted({key for key in map(name_key, [name, *aliases]) if key})


def entity_id(entity_type: str, name: str) -> str:
    """id сущности: первые 16 hex-символов sha1 от `<тип в casefold>|<normalize_name(name)>`.

    Совпадение `entity_id` — точное совпадение, при котором resolver сливает сущности
    без проверки. sha1 здесь не для криптостойкости, а ради коротких стабильных id."""
    key = f"{entity_type.casefold()}|{normalize_name(name)}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def slugify(name: str) -> str:
    """Имя файла страницы wiki (без `.md`) и цель `[[ссылок]]`: нормализованное имя через
    дефисы, "#" -> "sharp", пустое — "untitled".

    Длинный slug обрезается до `_SLUG_MAX_CHARS` символов и `_SLUG_MAX_BYTES` байтов UTF-8
    (кириллица — 2 байта на букву) с суффиксом-хэшем полного slug. Уникальность не
    гарантирована: разные имена могут дать один slug, поэтому `WikiBuilder` при
    конфликте добавляет к нему тип или начало `entity_id`."""
    # "#" начинает фрагмент URL, поэтому он не должен попасть в имя файла или ссылку wiki
    slug = normalize_name(name).replace("#", "sharp").replace(" ", "-")
    if not slug:
        return "untitled"
    if len(slug) <= _SLUG_MAX_CHARS and len(slug.encode("utf-8")) <= _SLUG_MAX_BYTES:
        return slug
    # обрезали: добавляем хэш полного slug, чтобы разные длинные имена не совпали
    suffix = "-" + hashlib.sha1(slug.encode("utf-8")).hexdigest()[:8]
    head = slug[: _SLUG_MAX_CHARS - len(suffix)]
    while len(head.encode("utf-8")) > _SLUG_MAX_BYTES - len(suffix):
        head = head[:-1]
    return head.rstrip("-") + suffix


def point_id(key: str) -> str:
    """id точки Qdrant: `uuid5(NAMESPACE, key)` строкой; ключ — chunk_id, entity_id и т. п."""
    return str(uuid.uuid5(NAMESPACE, key))
