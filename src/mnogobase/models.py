from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Modality = Literal["text", "image", "audio"]
ContextKind = Literal["chunk", "wiki", "relation", "entity"]


class EmbedInput(BaseModel):
    """One thing to embed. Only `text` is implemented; image/audio are reserved."""

    modality: Modality = "text"
    text: str | None = None
    path: Path | None = None
    title: str | None = None


class DocumentRecord(BaseModel):
    doc_id: str
    path: str
    title: str
    mime: str
    n_pages: int | None = None


class ChunkRecord(BaseModel):
    chunk_id: str
    doc_id: str
    idx: int
    text: str
    context_text: str
    headings: list[str] = Field(default_factory=list)
    page_start: int | None = None
    page_end: int | None = None
    n_tokens: int = 0
    modality: Modality = "text"
    path: str = ""


# --- LLM extraction schemas (sent to the model as strict JSON Schema) ---


class ExtractedEntity(BaseModel):
    name: str
    type: str
    description: str
    aliases: list[str]


class ExtractedRelation(BaseModel):
    source: str
    target: str
    predicate: str
    description: str
    strength: int


class ExtractionResult(BaseModel):
    entities: list[ExtractedEntity]
    relations: list[ExtractedRelation]


# --- graph / wiki records ---


class EntityRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    entity_id: str
    name: str
    type: str
    aliases: list[str] = Field(default_factory=list)
    description: str = ""
    descriptions: list[str] = Field(default_factory=list)
    mention_count: int = 0


class RelationView(BaseModel):
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
    entity: EntityRecord
    relations: list[RelationView] = Field(default_factory=list)


class ChunkView(BaseModel):
    chunk_id: str
    doc_id: str
    text: str
    path: str | None = None
    page: int | None = None
    headings: list[str] = Field(default_factory=list)


class WikiPageRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    page_id: str
    slug: str
    title: str
    path: str  # relative to the wiki dir, e.g. "entities/transformer.md"
    content_hash: str = ""
    version: int = 1


class PageIndexRow(BaseModel):
    page_id: str
    slug: str
    title: str
    path: str
    entity_type: str
    aliases: list[str] = Field(default_factory=list)


# --- retrieval ---


class SearchHit(BaseModel):
    key: str
    score: float
    payload: dict[str, Any] = Field(default_factory=dict)


class ContextItem(BaseModel):
    kind: ContextKind
    ref: str
    text: str
    path: str | None = None
    page: int | None = None
    score: float = 0.0


class Source(BaseModel):
    n: int
    kind: ContextKind
    ref: str
    path: str | None = None
    page: int | None = None
    snippet: str = ""
    cited: bool = False


class Answer(BaseModel):
    question: str
    mode: str
    text: str
    sources: list[Source] = Field(default_factory=list)
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    context_tokens: int = 0
