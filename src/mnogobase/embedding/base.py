"""Протоколы эмбеддеров и общие помощники векторизации.

`Embedder` (dense) и `SparseEncoder` (BM25) — `typing.Protocol`: реализации подставляются
через `build_app`, тесты используют `FakeEmbedder` / `FakeSparse`. Dense-векторы чанков,
wiki-секций и сущностей лежат в Qdrant вместе со sparse-векторами для гибридного поиска.

Сигнатура эмбеддера. Векторы в индексе сравнимы только с векторами той же модели, той же
размерности и тех же шаблонов документа/запроса. Поэтому сигнатура
`model_id:dim:tpl-<хеш шаблонов>` (`model_id` = `<serve>:<weights>`) хранится в `meta`
реестра; при расхождении (`signature_mismatch`) ingest и ask блокируются до
`mnogobase reindex`.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from typing import Protocol

from qdrant_client import models as qm

from mnogobase.core.models import EmbedInput


class Embedder(Protocol):
    """Dense-эмбеддер: документы и запросы векторизуются разными шаблонами (асимметричные
    модели вроде Qwen3-Embedding ждут инструкцию только у запроса).

    `model_id`, `dim` и `templates` входят в сигнатуру индекса (`embedder_signature`)."""

    model_id: str
    dim: int
    templates: tuple[str, str]  # шаблоны (документа, запроса), которые применяет эмбеддер

    def embed_documents(self, items: Sequence[EmbedInput]) -> list[list[float]]:
        """Векторы для `items` в порядке входа (шаблон документа)."""
        ...

    def embed_query(self, query: str) -> list[float]:
        """Вектор поискового запроса (шаблон запроса)."""
        ...


class SparseEncoder(Protocol):
    """Разреженный (BM25) кодировщик для гибридного поиска в Qdrant."""

    def encode_documents(self, texts: Sequence[str]) -> list[qm.SparseVector]:
        """Sparse-векторы документов в порядке входа."""
        ...

    def encode_query(self, text: str) -> qm.SparseVector:
        """Sparse-вектор запроса."""
        ...


def format_document(item: EmbedInput, template: str) -> str:
    """Текст документа для эмбеддера по `embedder.doc_template` (`{title}`, `{text}`).

    Без заголовка подставляется `none`. Пока поддерживается только текст: другая
    модальность или пустой `text` — `NotImplementedError`."""
    if item.modality != "text" or item.text is None:
        raise NotImplementedError(f"модальность {item.modality!r} пока не поддерживается")
    return template.format(title=item.title or "none", text=item.text)


def format_query(query: str, template: str) -> str:
    """Текст запроса для эмбеддера по `embedder.query_template` (`{query}`)."""
    return template.format(query=query)


def truncate_normalize(vec: Sequence[float], dim: int) -> list[float]:
    """Matryoshka-усечение: оставляет первые `dim` значений и заново нормирует вектор.

    `ValueError`, если сервер вернул меньше `dim` значений или вектор нулевой / содержит
    NaN или бесконечность (такой вектор испортил бы поиск по косинусу)."""
    if len(vec) < dim:
        raise ValueError(f"модель вернула вектор размерности {len(vec)}, а в конфиге {dim}")
    head = list(vec[:dim])
    norm = math.sqrt(sum(x * x for x in head))
    if not norm or not math.isfinite(norm):
        raise ValueError("эмбеддинг нулевой или содержит нечисловые значения")
    return [x / norm for x in head]


def embedder_signature(embedder: Embedder) -> str:
    """От чего зависят сохранённые векторы: `model_id:dim:tpl-<хеш обоих шаблонов>`.

    Обращение к `embedder.dim` при `dim: auto` делает пробный запрос к серверу."""
    templates = json.dumps(list(embedder.templates), ensure_ascii=False)
    digest = hashlib.sha256(templates.encode("utf-8")).hexdigest()[:8]
    return f"{embedder.model_id}:{embedder.dim}:tpl-{digest}"


def signature_mismatch(stored: str | None, embedder: Embedder) -> str | None:
    """Почему индекс с сигнатурой `stored` нельзя использовать с `embedder`; None, если можно.

    Отсутствие сигнатуры (индекса ещё нет) допустимо: её запишет первый `prepare`. Если
    модель и размерность совпадают, а различается только хеш, в причину добавляется
    подсказка про `doc_template` / `query_template`."""
    current = embedder_signature(embedder)
    if stored in (None, current):
        return None
    reason = f"индекс построен с {stored}, а в конфиге сейчас {current}"
    if stored.startswith(f"{embedder.model_id}:{embedder.dim}:tpl-"):
        reason += " (изменились шаблоны эмбеддера: doc_template / query_template)"
    return reason
