from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Iterable, Mapping

from mnogobase.ids import normalize_name
from mnogobase.llm.client import LLMClient
from mnogobase.llm.templates import render
from mnogobase.log import get_logger
from mnogobase.models import ChunkRecord, ExtractedEntity, ExtractedRelation, ExtractionResult
from mnogobase.registry import Registry

PROMPT_VERSION = "extract-v2"
TYPES_CHANGED = (
    "entity types changed in config: already-ingested documents keep their old types "
    "(new and changed documents use the new ones); run `mnogobase reset`, then "
    "`mnogobase ingest`, to re-extract everything"
)


def entity_types_signature(entity_types: Mapping[str, str]) -> str:
    """Short stable hash of the type names, their order and descriptions."""
    payload = json.dumps(list(entity_types.items()), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _cache_version(entity_types: Mapping[str, str]) -> str:
    # the cache key: a changed type set (names, order or descriptions) means extracting again
    return f"{PROMPT_VERSION}:{entity_types_signature(entity_types)}"


def stale_entity_types(registry: Registry, entity_types: Mapping[str, str]) -> bool:
    """True when some chunk's latest extraction was made with another type set.

    Only the types part of the cache version counts (a new prompt version with the same
    types is not a type change); a version without it predates typed keys."""
    signature = entity_types_signature(entity_types)
    for version in registry.latest_extraction_versions():
        prompt, sep, types = version.rpartition(":")
        if not sep or not prompt or types != signature:
            return True
    return False


def format_entity_types(entity_types: Mapping[str, str]) -> str:
    return "\n".join(f"- {n}: {d}" if d else f"- {n}" for n, d in entity_types.items())


def clean_extraction(result: ExtractionResult, entity_types: Iterable[str]) -> ExtractionResult:
    """Normalize types, merge duplicate names, resolve aliases in relations, drop bad edges.

    An unknown type becomes `Other` if that type exists, else the last type."""
    names = list(entity_types)
    allowed = {t.casefold(): t for t in names}
    fallback = allowed.get("other", names[-1])
    entities: dict[str, ExtractedEntity] = {}
    lookup: dict[str, str] = {}  # normalized name or alias -> entity key
    for e in result.entities:
        key = normalize_name(e.name)
        if not key:
            continue
        aliases = {a.strip() for a in e.aliases if a.strip() and normalize_name(a) != key}
        if key in entities:
            prev = entities[key]
            prev.aliases = sorted(set(prev.aliases) | aliases)
        else:
            entities[key] = ExtractedEntity(
                name=e.name.strip(),
                type=allowed.get(e.type.strip().casefold(), fallback),
                description=e.description.strip(),
                aliases=sorted(aliases),
            )
        lookup[key] = key
        for alias in aliases:
            lookup.setdefault(normalize_name(alias), key)

    relations: list[ExtractedRelation] = []
    seen: set[tuple[str, str, str]] = set()
    for r in result.relations:
        src = lookup.get(normalize_name(r.source))
        dst = lookup.get(normalize_name(r.target))
        if src is None or dst is None or src == dst:
            continue
        predicate = normalize_name(r.predicate).replace(" ", "_") or "related_to"
        if (src, dst, predicate) in seen:
            continue
        seen.add((src, dst, predicate))
        relations.append(
            ExtractedRelation(
                source=entities[src].name,
                target=entities[dst].name,
                predicate=predicate,
                description=r.description.strip(),
                strength=min(10, max(1, r.strength)),
            )
        )
    return ExtractionResult(entities=list(entities.values()), relations=relations)


class Extractor:
    def __init__(self, llm: LLMClient, registry: Registry, entity_types: Mapping[str, str]):
        self._llm = llm
        self._registry = registry
        self._types = dict(entity_types)
        self._version = _cache_version(self._types)
        self._log = get_logger(__name__)

    @property
    def model(self) -> str:
        return self._llm.model_for("extract")

    def cached(self, chunk: ChunkRecord, any_version: bool = False) -> ExtractionResult | None:
        """The cached extraction for the current model, prompt version and entity types; with
        `any_version`, fall back to the latest one made by any model, prompt or types.

        Every hit is cleaned with the current types, so a type that is no longer configured
        never reaches an entity_id (it becomes `Other`)."""
        raw = self._registry.get_extraction(chunk.chunk_id, self._version, self.model)
        if raw is None and any_version:
            raw = self._registry.get_latest_extraction(chunk.chunk_id)
        if raw is None:
            return None
        return clean_extraction(ExtractionResult.model_validate_json(raw), self._types)

    async def extract(self, chunk: ChunkRecord, title: str) -> ExtractionResult:
        hit = self.cached(chunk)
        if hit is not None:
            return hit
        prompt = render(
            "extract",
            entity_types=format_entity_types(self._types),
            title=title,
            headings=" > ".join(chunk.headings) or "-",
            text=chunk.text,
        )
        raw = await self._llm.structured(
            [{"role": "user", "content": prompt}], ExtractionResult, task="extract"
        )
        result = clean_extraction(raw, self._types)
        self._registry.put_extraction(
            chunk.chunk_id, self._version, self.model, result.model_dump_json()
        )
        return result

    async def extract_many(
        self, chunks: list[ChunkRecord], title: str
    ) -> dict[str, ExtractionResult]:
        async def one(chunk: ChunkRecord) -> tuple[str, ExtractionResult | None]:
            try:
                result = await self.extract(chunk, title)
            except Exception as exc:  # one bad chunk must not fail the document
                self._registry.set_chunk_extract(
                    chunk.chunk_id, "failed", f"{type(exc).__name__}: {exc}"
                )
                self._log.warning(
                    "extract_failed",
                    chunk_id=chunk.chunk_id,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                return chunk.chunk_id, None
            self._registry.set_chunk_extract(chunk.chunk_id, "done")
            return chunk.chunk_id, result

        pairs = await asyncio.gather(*(one(c) for c in chunks))
        return {cid: r for cid, r in pairs if r is not None}
