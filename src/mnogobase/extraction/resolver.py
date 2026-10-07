from __future__ import annotations

from pydantic import BaseModel

from mnogobase.config import ResolveSettings
from mnogobase.embedding.base import Embedder
from mnogobase.ids import entity_id, normalize_name
from mnogobase.llm.client import LLMClient
from mnogobase.llm.templates import render
from mnogobase.log import get_logger
from mnogobase.models import EmbedInput, EntityRecord, ExtractedEntity
from mnogobase.stores.graph_store import GraphStore
from mnogobase.stores.qdrant_store import QdrantStore


class SameEntity(BaseModel):
    same: bool
    reason: str


class MergedDescription(BaseModel):
    description: str


def entity_embed_input(record: EntityRecord | ExtractedEntity) -> EmbedInput:
    """What gets embedded for an entity in the Qdrant `entities` collection."""
    return EmbedInput(text=f"{record.name}: {record.description}", title=record.name)


class EntityResolver:
    """Maps an extracted entity onto an existing graph entity or creates a new one."""

    def __init__(
        self,
        graph: GraphStore,
        vectors: QdrantStore,
        embedder: Embedder,
        llm: LLMClient,
        settings: ResolveSettings,
    ):
        self._graph = graph
        self._vectors = vectors
        self._embedder = embedder
        self._llm = llm
        self._s = settings
        self._resolved: dict[str, str] = {}  # extracted id -> canonical id (per process)
        self._log = get_logger(__name__)

    def _vector(self, record: EntityRecord | ExtractedEntity) -> list[float]:
        return self._embedder.embed_documents([entity_embed_input(record)])[0]

    async def resolve(self, entity: ExtractedEntity) -> EntityRecord:
        key = entity_id(entity.type, entity.name)
        existing = self._graph.get_entity(self._resolved.get(key, key))
        if existing is None:
            existing = await self._match(entity)
        if existing is None:
            record = EntityRecord(
                entity_id=key,
                name=entity.name,
                type=entity.type,
                aliases=sorted(set(entity.aliases)),
                description=entity.description,
                descriptions=[entity.description] if entity.description else [],
            )
            changed = True
        else:
            record, changed = await self._merge(existing, entity)
        if changed:
            # spec §7 order: vector first, then Qdrant, then Neo4j. An embedder or Qdrant
            # failure must not leave the graph ahead (a retry would see "no change").
            vector = self._vector(record)
            self._vectors.upsert_entities([record], [vector])
            self._graph.upsert_entity(record)
        self._resolved[key] = record.entity_id
        return record

    async def _match(self, entity: ExtractedEntity) -> EntityRecord | None:
        hits = self._vectors.search_entities(
            self._vector(entity), k=3, score_threshold=self._s.llm_check
        )
        for hit in hits:
            candidate = self._graph.get_entity(hit.key)
            if candidate is None:
                continue
            if hit.score >= self._s.auto_merge:
                self._log.info(
                    "entity_merged",
                    name=entity.name,
                    into=candidate.name,
                    score=round(hit.score, 3),
                    method="vector",
                )
                return candidate
            if await self._same(entity, candidate):
                self._log.info(
                    "entity_merged",
                    name=entity.name,
                    into=candidate.name,
                    score=round(hit.score, 3),
                    method="llm",
                )
                return candidate
        return None

    async def _same(self, entity: ExtractedEntity, candidate: EntityRecord) -> bool:
        prompt = render(
            "resolve_same",
            a_name=entity.name,
            a_type=entity.type,
            a_description=entity.description,
            b_name=candidate.name,
            b_type=candidate.type,
            b_description=candidate.description,
        )
        verdict = await self._llm.structured(
            [{"role": "user", "content": prompt}], SameEntity, task="resolve"
        )
        return verdict.same

    async def _merge(
        self, existing: EntityRecord, entity: ExtractedEntity
    ) -> tuple[EntityRecord, bool]:
        aliases = set(existing.aliases) | set(entity.aliases)
        if normalize_name(entity.name) != normalize_name(existing.name):
            aliases.add(entity.name)
        descriptions = list(existing.descriptions)
        if entity.description and entity.description not in descriptions:
            descriptions.append(entity.description)
        changed = aliases != set(existing.aliases) or descriptions != existing.descriptions
        if not changed:
            return existing, False
        if len(descriptions) > self._s.max_descriptions:
            prompt = render(
                "resolve_summarize",
                name=existing.name,
                type=existing.type,
                descriptions="\n".join(f"- {d}" for d in descriptions),
            )
            merged = await self._llm.structured(
                [{"role": "user", "content": prompt}], MergedDescription, task="resolve"
            )
            descriptions = [merged.description]
        return existing.model_copy(
            update={
                "aliases": sorted(aliases),
                "descriptions": descriptions,
                "description": " ".join(descriptions),
            }
        ), True
