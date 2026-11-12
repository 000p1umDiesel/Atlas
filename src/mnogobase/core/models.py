"""Pydantic-модели данных, которыми обмениваются слои: парсинг, чанкинг, извлечение,
хранилища, поиск и ответ.

Поток: `DoclingParser` даёт `DocumentRecord` и текст -> `Chunker` режет его на
`ChunkRecord` -> `Extractor` получает от LLM `ExtractionResult` (сущности и связи) ->
`GraphStore` хранит и отдаёт `EntityRecord` / `RelationView` / `WikiPageRecord` ->
retriever'ы собирают `ContextItem` -> `Answerer` возвращает `Answer` с `Source`.

Модели извлечения (`ExtractedEntity`, `ExtractedRelation`, `ExtractionResult`) передаются
LLM как strict JSON Schema (`llm/client.py:strict_json_schema`). Их форма — часть
контракта с моделью: менять поля можно только вместе с `PROMPT_VERSION` извлечения.
У них намеренно нет docstring'ов: pydantic кладёт docstring класса в `description` схемы,
и он ушёл бы в запрос к LLM.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# вид содержимого; пока реализован только text
Modality = Literal["text", "image", "audio"]
# откуда взят фрагмент контекста: чанк документа, страница wiki, связь или сущность графа
ContextKind = Literal["chunk", "wiki", "relation", "entity"]


class EmbedInput(BaseModel):
    """Один объект для эмбеддинга. Реализован только `text`; image/audio зарезервированы."""

    modality: Modality = "text"
    text: str | None = None
    path: Path | None = None
    title: str | None = None


class DocumentRecord(BaseModel):
    """Разобранный документ: id по содержимому, путь, заголовок, MIME-тип и число
    страниц (`None` у форматов без страниц)."""

    doc_id: str
    path: str
    title: str
    mime: str
    n_pages: int | None = None


class ChunkRecord(BaseModel):
    """Чанк документа. Хранится в кэше чанков, в Qdrant (`mb_chunks`) и как узел Neo4j.

    `text` — сам фрагмент (его видит пользователь в цитатах, по нему идёт извлечение),
    `context_text` — он же с заголовками разделов: именно он эмбеддится и по нему
    считается `n_tokens`. `idx` — порядковый номер в документе, страницы — диапазон
    страниц исходника, если они известны."""

    chunk_id: str
    doc_id: str
    idx: int
    text: str
    context_text: str
    headings: list[str] = Field(default_factory=list)  # заголовки разделов над чанком
    page_start: int | None = None
    page_end: int | None = None
    n_tokens: int = 0  # размер context_text по токенизатору эмбеддера
    modality: Modality = "text"
    path: str = ""  # путь исходного файла (для цитат)


# --- схемы LLM-извлечения (передаются модели как strict JSON Schema) ---
# Без docstring'ов: pydantic превратил бы их в `description` схемы, отправляемой в LLM.


# Сущность из одного чанка: каноническое английское имя, тип из `extract.entity_types`,
# описание по тексту чанка и псевдонимы (включая написание в оригинале).
class ExtractedEntity(BaseModel):
    name: str
    type: str
    description: str
    aliases: list[str]


# Связь между двумя сущностями того же чанка; `source` / `target` — их имена,
# `strength` — сила связи по оценке модели (1..10, шкалу задаёт промпт extract.md).
class ExtractedRelation(BaseModel):
    source: str
    target: str
    predicate: str
    description: str
    strength: int


# Полный ответ LLM на извлечение для одного чанка; кэшируется в registry.
class ExtractionResult(BaseModel):
    entities: list[ExtractedEntity]
    relations: list[ExtractedRelation]


# --- записи графа / wiki ---


class EntityRecord(BaseModel):
    """Сущность графа (узел `Entity` в Neo4j). Лишние свойства узла игнорируются.

    `description` — сводное описание, `descriptions` — описания из отдельных чанков, из
    которых оно собрано; `mention_count` — число упоминаний в чанках."""

    model_config = ConfigDict(extra="ignore")

    entity_id: str
    name: str
    type: str
    aliases: list[str] = Field(default_factory=list)
    description: str = ""
    descriptions: list[str] = Field(default_factory=list)
    mention_count: int = 0


class RelationView(BaseModel):
    """Связь графа в удобном для промпта виде: обе стороны с id и именами, предикат,
    `weight` — сумма сил связи по всем подтверждающим чанкам, `evidence` — их chunk_id."""

    model_config = ConfigDict(extra="ignore")

    src_id: str
    src_name: str
    predicate: str
    dst_id: str
    dst_name: str
    description: str = ""
    weight: float = 0.0
    evidence: list[str] = Field(default_factory=list)


class EntityContext(BaseModel):
    """Сущность вместе с её связями — материал для страницы wiki и ответов API."""

    entity: EntityRecord
    relations: list[RelationView] = Field(default_factory=list)


class ChunkView(BaseModel):
    """Чанк, прочитанный из графа для показа или цитирования: текст, путь к файлу,
    первая страница (`page_start`) и заголовки."""

    chunk_id: str
    doc_id: str
    text: str
    path: str | None = None
    page: int | None = None
    headings: list[str] = Field(default_factory=list)


class WikiPageRecord(BaseModel):
    """Учётная запись страницы wiki (узел `WikiPage` в Neo4j); `page_id` = `entity_id`.

    `content_hash` — sha1 записанного текста страницы, `version` растёт с каждой
    пересборкой страницы."""

    model_config = ConfigDict(extra="ignore")

    page_id: str
    slug: str
    title: str
    path: str  # относительно каталога wiki, например "entities/transformer.md"
    content_hash: str = ""
    version: int = 1
    stale: bool = False  # файл и векторы удалены до пересборки (GraphStore.invalidate_wiki_page)


class PageIndexRow(BaseModel):
    """Страница wiki со сведениями о сущности (тип, псевдонимы) — для `index.md`,
    reindex и списка страниц в API (`GraphStore.wiki_pages`)."""

    page_id: str
    slug: str
    title: str
    path: str
    entity_type: str
    aliases: list[str] = Field(default_factory=list)


# --- поиск и ответы ---


class SearchHit(BaseModel):
    """Хит векторного поиска Qdrant: ключ точки (chunk_id, page_id, entity_id), оценка и
    payload точки."""

    key: str
    score: float
    payload: dict[str, Any] = Field(default_factory=dict)


class ContextItem(BaseModel):
    """Фрагмент контекста, который retriever отдаёт на ответ.

    `ref` — id источника (chunk_id, page_id, id сущности или связи), `path` / `page` —
    откуда цитировать, `score` — оценка для сортировки и обрезки по бюджету токенов."""

    kind: ContextKind
    ref: str
    text: str
    path: str | None = None
    page: int | None = None
    score: float = 0.0


class Source(BaseModel):
    """Источник в ответе: `n` — номер, на который ссылается текст ответа (`[n]`),
    `snippet` — начало фрагмента (до 200 символов) для показа, `cited` — сослался ли
    ответ на него."""

    n: int
    kind: ContextKind
    ref: str
    path: str | None = None
    page: int | None = None
    snippet: str = ""
    cited: bool = False


class Answer(BaseModel):
    """Результат `ask`: текст ответа, источники и служебные метрики.

    `contexts` нужны для оценки Ragas, токены и `latency_ms` — для сравнения режимов."""

    question: str
    mode: str
    text: str
    sources: list[Source] = Field(default_factory=list)
    contexts: list[str] = Field(default_factory=list)  # полные блоки из промпта после бюджета
    alt_queries: list[str] = Field(default_factory=list)  # напр., английский перевод для поиска
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    context_tokens: int = 0  # размер контекста в промпте ответа
