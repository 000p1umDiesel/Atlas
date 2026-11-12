"""Сопоставление сущностей (entity resolution) на стадии `graph` конвейера ingest.

Место в потоке данных: `extract` даёт для каждого чанка сущности с именем, типом, описанием и
алиасами; стадия `graph` прогоняет каждую через `EntityResolver.resolve`, получает
каноническую `EntityRecord` и уже по её `entity_id` пишет упоминания и связи чанка
(`GraphStore.add_chunk_facts`).

Порядок поиска существующей сущности:
1. **Точный id.** `entity_id = hash(тип + normalize_name(имя))` (`core/ids.py`). Если такая
   сущность уже есть в Neo4j (или в этом документе её уже сопоставили с другой — кэш
   `_resolved`), она и берётся.
2. **Кандидаты по имени и алиасам.** `name_key` (нормализованное имя без пробелов и дефисов:
   "MagAO-X" = "MagAOX") от имени и всех алиасов ищется в payload `name_keys` коллекции
   Qdrant `entities` (`find_entities_by_name`). Так находится та же сущность под другим
   типом или с другим написанием.
3. **Кандидаты по вектору.** Эмбеддинг «имя: описание» ищется в той же коллекции с порогом
   косинуса `resolve.llm_check`.
4. **Решение.** Векторный кандидат со score ≥ `resolve.auto_merge` сливается сразу, но по
   умолчанию `auto_merge = 1.0`, так что на практике каждый кандидат (и по имени, и по
   вектору) проверяет LLM промптом `resolve_same.md`: чисто векторное слияние склеивало,
   например, "X. Zhang" и "H. Zhang". Берётся первый кандидат, которого LLM признал той же
   сущностью; кандидаты по имени проверяются раньше векторных.
5. **Слияние.** Найденная сущность получает новое имя как алиас и новое описание в
   `descriptions`. Когда описаний больше `resolve.max_descriptions`, LLM сводит их в одно
   (`resolve_summarize.md`). Ничего не нашлось — создаётся новая сущность с исходным id.

Сущности извлекаются под каноническими английскими именами, а исходное написание остаётся
алиасом, поэтому русские и английские тексты сходятся к одним узлам.

Порядок записи (инвариант проекта): сначала вектор, затем Qdrant, затем Neo4j. Если упал
эмбеддер или Qdrant, граф не окажется впереди, и повтор стадии не увидит «нет изменений».
"""

from __future__ import annotations

from pydantic import BaseModel

from mnogobase.core.config import ResolveSettings
from mnogobase.core.ids import entity_id, entity_name_keys, normalize_name
from mnogobase.core.log import get_logger
from mnogobase.core.models import EmbedInput, EntityRecord, ExtractedEntity
from mnogobase.embedding.base import Embedder
from mnogobase.llm.client import LLMClient
from mnogobase.llm.templates import render_messages
from mnogobase.stores.graph_store import GraphStore
from mnogobase.stores.qdrant_store import QdrantStore

_CANDIDATES = 3  # сколько кандидатов брать по имени и отдельно по вектору


class SameEntity(BaseModel):
    """Ответ LLM на `resolve_same.md`: одна ли это сущность, и почему."""

    same: bool
    reason: str


class MergedDescription(BaseModel):
    """Ответ LLM на `resolve_summarize.md`: одно сводное описание вместо нескольких."""

    description: str


def entity_embed_input(record: EntityRecord | ExtractedEntity) -> EmbedInput:
    """Что эмбеддится для сущности в коллекции Qdrant `entities`: «имя: описание»."""
    return EmbedInput(text=f"{record.name}: {record.description}", title=record.name)


class EntityResolver:
    """Сопоставляет извлечённую сущность с существующей сущностью графа или создаёт новую.

    Пишет в Qdrant (коллекция `entities`) и Neo4j (узел `Entity`), вызывает эмбеддер и LLM
    (задача `resolve`). Состояние между вызовами — только кэш `_resolved` в пределах
    одного документа; его сбрасывает `forget`."""

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
        self._resolved: dict[str, str] = {}  # извлечённый id -> канонический id (на документ)
        self._log = get_logger(__name__)

    def forget(self) -> None:
        """Сбрасывает кэш сопоставлений: в долгоживущем worker сущности между задачами
        удаляются и пересоздаются, и старое сопоставление могло бы увести не туда.

        Стадия `graph` вызывает его в начале каждого документа."""
        self._resolved.clear()

    def _vector(self, record: EntityRecord | ExtractedEntity) -> list[float]:
        """Dense-вектор сущности (сетевой вызов эмбеддера)."""
        return self._embedder.embed_documents([entity_embed_input(record)])[0]

    async def resolve(self, entity: ExtractedEntity) -> EntityRecord:
        """Возвращает каноническую сущность для `entity`, при необходимости создав или
        дополнив её.

        Побочные эффекты: если сущность новая или у неё изменились алиасы/описания —
        эмбеддинг, upsert в Qdrant `entities` и `MERGE` узла `Entity` в Neo4j (в этом
        порядке). Без изменений ничего не пишется. Вызовы LLM — для проверки кандидатов и
        сводки описаний. Исключения эмбеддера, LLM, Qdrant и Neo4j пробрасываются."""
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
            # порядок записи: сначала вектор, затем Qdrant, затем Neo4j. Сбой эмбеддера
            # или Qdrant не должен оставить граф впереди (повтор увидел бы "нет изменений").
            vector = self._vector(record)
            self._vectors.upsert_entities([record], [vector])
            self._graph.upsert_entity(record)
        self._resolved[key] = record.entity_id
        return record

    async def _match(self, entity: ExtractedEntity) -> EntityRecord | None:
        """Ищет существующую сущность среди кандидатов по имени/алиасам и по вектору.

        Кандидаты по имени идут первыми и без score, векторные — после них, без повторов.
        Кандидат, которого уже нет в графе (Qdrant отстал от удаления), пропускается.
        Возвращает первого подтверждённого кандидата или None; слияние логируется
        (`entity_merged`, `method` = vector | llm | llm_name)."""
        # Совпадение имени или алиаса проверяется LLM при любом косинусе: та же сущность под
        # другим типом или с другим описанием часто оказывается ниже llm_check.
        by_name = self._vectors.find_entities_by_name(
            entity_name_keys(entity.name, entity.aliases), limit=_CANDIDATES
        )
        hits = self._vectors.search_entities(
            self._vector(entity), k=_CANDIDATES, score_threshold=self._s.llm_check
        )
        candidates: list[tuple[str, float | None]] = [(key, None) for key in by_name]
        candidates += [(hit.key, hit.score) for hit in hits if hit.key not in by_name]
        for key, score in candidates:
            candidate = self._graph.get_entity(key)
            if candidate is None:
                continue
            if score is not None and score >= self._s.auto_merge:
                method = "vector"
            elif await self._same(entity, candidate):
                method = "llm" if score is not None else "llm_name"
            else:
                continue
            self._log.info(
                "entity_merged",
                name=entity.name,
                into=candidate.name,
                score=None if score is None else round(score, 3),
                method=method,
            )
            return candidate
        return None

    async def _same(self, entity: ExtractedEntity, candidate: EntityRecord) -> bool:
        """Спрашивает LLM (`resolve_same.md`), одна ли это сущность: сравниваются имя, тип и
        описание обеих."""
        messages = render_messages(
            "resolve_same",
            a_name=entity.name,
            a_type=entity.type,
            a_description=entity.description,
            b_name=candidate.name,
            b_type=candidate.type,
            b_description=candidate.description,
        )
        verdict = await self._llm.structured(messages, SameEntity, task="resolve")
        return verdict.same

    async def _merge(
        self, existing: EntityRecord, entity: ExtractedEntity
    ) -> tuple[EntityRecord, bool]:
        """Дополняет `existing` алиасами и описанием из `entity`.

        Имя и тип существующей сущности не меняются (иначе изменился бы её `entity_id`);
        новое имя, если оно отличается после нормализации, становится алиасом. `description` —
        все `descriptions` через пробел; при переполнении `max_descriptions` они заменяются
        одной LLM-сводкой. Возвращает (запись, изменилось ли что-то); сама ничего не пишет."""
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
            messages = render_messages(
                "resolve_summarize",
                name=existing.name,
                type=existing.type,
                descriptions="\n".join(f"- {d}" for d in descriptions),
            )
            merged = await self._llm.structured(messages, MergedDescription, task="resolve")
            descriptions = [merged.description]
        return existing.model_copy(
            update={
                "aliases": sorted(aliases),
                "descriptions": descriptions,
                "description": " ".join(descriptions),
            }
        ), True
