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
from mnogobase.progress import NULL_PROGRESS, ProgressSink
from mnogobase.registry import Registry

PROMPT_VERSION = "extract-v2"
TYPES_CHANGED = (
    "entity types changed in config: already-ingested documents keep their old types "
    "(new and changed documents use the new ones); run `mnogobase reset`, then "
    "`mnogobase ingest`, to re-extract everything"
)


def entity_types_signature(entity_types: Mapping[str, str]) -> str:
    """Короткий стабильный хеш имён типов, их порядка и описаний."""
    payload = json.dumps(list(entity_types.items()), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _cache_version(entity_types: Mapping[str, str]) -> str:
    # ключ кэша: изменился набор типов (имена, порядок или описания) — извлекаем заново
    return f"{PROMPT_VERSION}:{entity_types_signature(entity_types)}"


def stale_entity_types(registry: Registry, entity_types: Mapping[str, str]) -> bool:
    """True, если последнее извлечение какого-либо чанка сделано с другим набором типов.

    Учитывается только часть версии кэша, отвечающая за типы (новая версия промпта с теми же
    типами — не смена типов); версия без неё появилась раньше ключей с типами."""
    signature = entity_types_signature(entity_types)
    for version in registry.latest_extraction_versions():
        prompt, sep, types = version.rpartition(":")
        if not sep or not prompt or types != signature:
            return True
    return False


def format_entity_types(entity_types: Mapping[str, str]) -> str:
    return "\n".join(f"- {n}: {d}" if d else f"- {n}" for n, d in entity_types.items())


def clean_extraction(result: ExtractionResult, entity_types: Iterable[str]) -> ExtractionResult:
    """Нормализует типы, сливает дубли имён, разрешает алиасы в связях, отбрасывает плохие рёбра.

    Неизвестный тип становится `Other`, если такой тип есть, иначе — последним типом."""
    names = list(entity_types)
    allowed = {t.casefold(): t for t in names}
    fallback = allowed.get("other", names[-1])
    entities: dict[str, ExtractedEntity] = {}
    lookup: dict[str, str] = {}  # нормализованное имя или алиас -> ключ сущности
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
        """Закэшированное извлечение для текущей модели, версии промпта и типов сущностей; с
        `any_version` — откат на последнее, сделанное любой моделью, промптом или типами.

        Каждое попадание чистится с текущими типами, так что тип, которого больше нет в
        конфиге, никогда не попадает в entity_id (он становится `Other`)."""
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
        self, chunks: list[ChunkRecord], title: str, progress: ProgressSink = NULL_PROGRESS
    ) -> dict[str, ExtractionResult]:
        """Извлекает все чанки параллельно; `progress` продвигается по завершении каждого
        (включая попадания в кэш и ошибки)."""

        async def one(chunk: ChunkRecord) -> tuple[str, ExtractionResult | None]:
            try:
                result = await self.extract(chunk, title)
            except Exception as exc:  # один плохой чанк не должен ронять документ
                self._registry.set_chunk_extract(
                    chunk.chunk_id, "failed", f"{type(exc).__name__}: {exc}"
                )
                self._log.warning(
                    "extract_failed",
                    chunk_id=chunk.chunk_id,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                progress.advance(failed=True)
                return chunk.chunk_id, None
            self._registry.set_chunk_extract(chunk.chunk_id, "done")
            progress.advance()
            return chunk.chunk_id, result

        pairs = await asyncio.gather(*(one(c) for c in chunks))
        return {cid: r for cid, r in pairs if r is not None}
