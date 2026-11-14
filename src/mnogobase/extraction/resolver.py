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

Сам `resolve` последовательный: каждая сущность видит записанные до неё. Чтобы его вызовы LLM
(секунды каждый) не шли по одному, стадия `graph` сначала вызывает `prefetch`: тот же выбор
кандидатов, но для всех новых сущностей документа сразу, с параллельными вопросами
`resolve_same`. Ответы кэшируются до `forget`, и `resolve` идёт к LLM только за промахами.

Сущности извлекаются под каноническими английскими именами, а исходное написание остаётся
алиасом, поэтому русские и английские тексты сходятся к одним узлам.

Порядок записи (инвариант проекта): сначала вектор, затем Qdrant, затем Neo4j. Если упал
эмбеддер или Qdrant, граф не окажется впереди, и повтор стадии не увидит «нет изменений».
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Sequence

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
        # заготовки `prefetch` на документ: ответы LLM (id сущности, id кандидата) -> same
        # и векторы по тексту входа эмбеддера
        self._verdicts: dict[tuple[str, str], bool] = {}
        self._vector_cache: dict[tuple[str | None, str | None], list[float]] = {}
        self.verdict_hits = 0  # ответов `_same`, взятых из заготовок (на документ)
        self.verdict_misses = 0  # ответов `_same`, за которыми пришлось идти в LLM
        self._log = get_logger(__name__)

    def forget(self) -> None:
        """Сбрасывает кэш сопоставлений и заготовки `prefetch`: в долгоживущем worker
        сущности между задачами удаляются и пересоздаются, и старое сопоставление могло бы
        увести не туда.

        Стадия `graph` вызывает его в начале каждого документа."""
        self._resolved.clear()
        self._verdicts.clear()
        self._vector_cache.clear()
        self.verdict_hits = self.verdict_misses = 0

    def _vector(self, record: EntityRecord | ExtractedEntity) -> list[float]:
        """Dense-вектор сущности: из заготовок `prefetch` или сетевым вызовом эмбеддера."""
        item = entity_embed_input(record)
        cached = self._vector_cache.get((item.text, item.title))
        return cached if cached is not None else self._embedder.embed_documents([item])[0]

    async def prefetch(self, entities: Sequence[ExtractedEntity]) -> int:
        """Заранее и параллельно задаёт LLM вопросы `resolve_same`, которые затем задаст
        последовательный `resolve` по сущностям документа `entities` (в порядке стадии).

        Сам `resolve` идёт строго по очереди, потому что каждая сущность видит уже записанные
        до неё; без заготовок его вызовы LLM (секунды каждый) тоже шли бы по одному.
        Здесь для первого появления каждого id, которого ещё нет в графе, одним запросом
        считаются векторы, а затем параллельно (в пределах `llm.concurrency`) проверяются:
        кандидаты из графа — в том же порядке и до первого «да», как в `_match`; более ранние
        новые сущности этого же документа, близкие по ключу имени или по косинусу не ниже
        `llm_check` (их `_match` увидит уже записанными). Ничего не пишет: ответы и векторы
        лишь кэшируются до `forget`, и `resolve` идёт к LLM только за тем, чего здесь не
        оказалось (кандидат изменился или появился иначе). Ошибка LLM или эмбеддера
        отменяет остальные вопросы и пробрасывается. Возвращает число заданных вопросов."""
        firsts: dict[str, ExtractedEntity] = {}
        for entity in entities:
            firsts.setdefault(entity_id(entity.type, entity.name), entity)
        keys = [key for key in firsts if self._graph.get_entity(key) is None]
        if not keys:
            return 0
        started = time.perf_counter()
        inputs = [entity_embed_input(firsts[key]) for key in keys]
        vectors = self._embedder.embed_documents(inputs)
        for item, vector in zip(inputs, vectors, strict=True):
            self._vector_cache[(item.text, item.title)] = vector
        norms = [math.sqrt(math.sumprod(v, v)) or 1.0 for v in vectors]
        name_keys = [set(entity_name_keys(firsts[k].name, firsts[k].aliases)) for k in keys]

        # Все обращения к Qdrant и Neo4j — здесь, синхронно; в задачах остаются только
        # ожидания LLM, которые и идут параллельно.
        chains: list[tuple[ExtractedEntity, list[EntityRecord]]] = []
        pairs: list[tuple[ExtractedEntity, str, ExtractedEntity]] = []
        for j, key in enumerate(keys):
            entity = firsts[key]
            chain = []
            for candidate_id, score in self._graph_candidates(entity):
                if score is not None and score >= self._s.auto_merge:
                    break  # `_match` сольёт по вектору без LLM, дальше не пойдёт
                candidate = self._graph.get_entity(candidate_id)
                if candidate is not None:
                    chain.append(candidate)
            chains.append((entity, chain))
            by_name = [i for i in range(j) if name_keys[i] & name_keys[j]][:_CANDIDATES]
            close = sorted(
                (
                    (math.sumprod(vectors[i], vectors[j]) / (norms[i] * norms[j]), i)
                    for i in range(j)
                    if i not in by_name
                ),
                reverse=True,
            )
            by_vector = [i for score, i in close[:_CANDIDATES] if score >= self._s.llm_check]
            pairs += [(entity, keys[i], firsts[keys[i]]) for i in by_name + by_vector]

        asked = 0

        async def walk(entity: ExtractedEntity, chain: list[EntityRecord]) -> None:
            nonlocal asked
            for candidate in chain:
                asked += 1
                same = await self._ask_same(entity, candidate)
                self._verdicts[(entity_id(entity.type, entity.name), candidate.entity_id)] = same
                if same:
                    return

        async def pair(entity: ExtractedEntity, other_id: str, other: ExtractedEntity) -> None:
            nonlocal asked
            asked += 1
            same = await self._ask_same(entity, other)
            self._verdicts[(entity_id(entity.type, entity.name), other_id)] = same

        try:
            async with asyncio.TaskGroup() as group:
                for entity, chain in chains:
                    if chain:
                        group.create_task(walk(entity, chain))
                for entity, other_id, other in pairs:
                    group.create_task(pair(entity, other_id, other))
        except ExceptionGroup as failed:
            # вызывающему нужен исходный тип (сбой LLM, сервис недоступен), а не группа
            raise failed.exceptions[0] from None
        self._log.info(
            "resolve_prefetch",
            entities=len(keys),
            asked=asked,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        return asked

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
        for key, score in self._graph_candidates(entity):
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

    def _graph_candidates(self, entity: ExtractedEntity) -> list[tuple[str, float | None]]:
        """Кандидаты для `entity` из Qdrant `entities`: (id, косинус).

        Сначала до `_CANDIDATES` совпадений по ключам имени и алиасов (без score), затем до
        `_CANDIDATES` векторных с косинусом не ниже `llm_check`, без повторов."""
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
        return candidates

    async def _same(self, entity: ExtractedEntity, candidate: EntityRecord) -> bool:
        """Одна ли это сущность: ответ из заготовок `prefetch` или вопрос к LLM."""
        cached = self._verdicts.get((entity_id(entity.type, entity.name), candidate.entity_id))
        if cached is not None:
            self.verdict_hits += 1
            return cached
        self.verdict_misses += 1
        return await self._ask_same(entity, candidate)

    async def _ask_same(
        self, entity: ExtractedEntity, candidate: EntityRecord | ExtractedEntity
    ) -> bool:
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
