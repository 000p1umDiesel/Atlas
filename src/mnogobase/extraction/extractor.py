"""Стадия `extract` конвейера ingest: LLM извлекает из каждого чанка сущности и связи.

Место в потоке данных: `chunk` → `embed` → **extract** → `graph` (сопоставление сущностей в
`extraction/resolver.py` и запись в Neo4j). Промпт — `llm/prompts/extract.md` (задача LLM
`extract`), форма ответа — pydantic-схема `ExtractionResult` (сущности с типом, описанием и
алиасами; связи источник → предикат → цель с описанием и силой 1..10). Текст чанка и его
заголовки передаются в user-сообщении как недоверенные данные; над маркером
`<!-- untrusted-data -->` остаются только настройки оператора (список типов сущностей).

Кэш извлечений лежит в реестре PostgreSQL (таблица `extraction_cache`) с ключом
`(chunk_id, prompt_version, model)`, где `prompt_version` = `PROMPT_VERSION:<хеш типов>`.
Поэтому повтор ingest того же файла не тратит вызовы LLM, а смена модели, промпта или
`extract.entity_types` приводит к новому извлечению только для новых и изменённых документов.

Инварианты:
- `PROMPT_VERSION` повышается при любом изменении смысла `extract.md`, иначе в кэше
  останутся ответы старого промпта под новой версией.
- Каждый результат (и свежий, и из кэша) проходит `clean_extraction` с текущими типами, поэтому
  в `entity_id` никогда не попадает тип, которого нет в конфиге.
- Смена `extract.entity_types` только предупреждает (`TYPES_CHANGED`): уже загруженные
  документы сохраняют старые типы до `reset` + `ingest`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Iterable, Mapping

from mnogobase.core.ids import normalize_name
from mnogobase.core.log import get_logger, safe_error
from mnogobase.core.models import ChunkRecord, ExtractedEntity, ExtractedRelation, ExtractionResult
from mnogobase.core.progress import NULL_PROGRESS, ProgressSink
from mnogobase.llm.client import LLMClient
from mnogobase.llm.templates import render_messages
from mnogobase.stores.registry import Registry

PROMPT_VERSION = "extract-v4"
TYPES_CHANGED = (
    "entity types changed in config: already-ingested documents keep their old types "
    "(new and changed documents use the new ones); run `mnogobase reset`, then "
    "`mnogobase ingest`, to re-extract everything"
)


def entity_types_signature(entity_types: Mapping[str, str]) -> str:
    """Короткий стабильный хеш имён типов, их порядка и описаний.

    Порядок важен: последний тип служит запасным для неизвестных (см. `clean_extraction`),
    а описания попадают в промпт и меняют ответы модели."""
    payload = json.dumps(list(entity_types.items()), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _cache_version(entity_types: Mapping[str, str]) -> str:
    # ключ кэша: изменился набор типов (имена, порядок или описания) — извлекаем заново
    return f"{PROMPT_VERSION}:{entity_types_signature(entity_types)}"


def stale_entity_types(registry: Registry, entity_types: Mapping[str, str]) -> bool:
    """True, если последнее извлечение какого-либо чанка сделано с другим набором типов.

    Учитывается только часть версии кэша после `:`, отвечающая за типы: новая версия промпта
    с теми же типами — не смена типов. Пустой кэш — False. Читает реестр PostgreSQL; по
    результату вызывающий код печатает предупреждение `TYPES_CHANGED`."""
    signature = entity_types_signature(entity_types)
    return any(
        version.rpartition(":")[2] != signature for version in registry.latest_extraction_versions()
    )


def format_entity_types(entity_types: Mapping[str, str]) -> str:
    """Список типов для промпта: по строке `- Имя: описание` (или `- Имя` без описания)."""
    return "\n".join(f"- {n}: {d}" if d else f"- {n}" for n, d in entity_types.items())


def clean_extraction(result: ExtractionResult, entity_types: Iterable[str]) -> ExtractionResult:
    """Нормализует типы, сливает дубли имён, разрешает алиасы в связях, отбрасывает плохие рёбра.

    Неизвестный тип становится `Other`, если такой тип есть, иначе — последним типом.

    Шаги:
    1. Сущности группируются по `normalize_name(name)`; сущность с пустым именем отбрасывается.
       У дубля берутся только алиасы, тип и описание остаются от первого вхождения. Алиас,
       совпадающий с самим именем после нормализации, выбрасывается.
    2. Строится таблица «нормализованное имя или алиас → сущность»; имя всегда важнее алиаса
       другой сущности (`setdefault`).
    3. Концы каждой связи ищутся по этой таблице; связь с неизвестным концом или петля
       (сущность сама с собой) отбрасывается. Предикат приводится к виду `snake_case`
       (пустой — `related_to`), дубли `(источник, цель, предикат)` убираются, сила
       зажимается в 1..10, а концы переписываются на канонические имена сущностей.

    Чистая функция: ничего не пишет и `result` не меняет, возвращает новый объект."""
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
    """Извлекает сущности и связи из чанков через LLM с кэшем в реестре.

    Аргументы: `llm` — клиент LLM (модель берётся по задаче `extract` из `llm.overrides`);
    `registry` — реестр PostgreSQL с таблицей `extraction_cache`; `entity_types` — словарь
    `extract.entity_types` «тип → описание» в порядке из конфига."""

    def __init__(self, llm: LLMClient, registry: Registry, entity_types: Mapping[str, str]):
        self._llm = llm
        self._registry = registry
        self._types = dict(entity_types)
        self._version = _cache_version(self._types)
        self._log = get_logger(__name__)

    @property
    def model(self) -> str:
        """Имя модели задачи `extract`: часть ключа кэша извлечений."""
        return self._llm.model_for("extract")

    def cached(self, chunk: ChunkRecord, any_version: bool = False) -> ExtractionResult | None:
        """Закэшированное извлечение для текущей модели, версии промпта и типов сущностей; с
        `any_version` — откат на последнее, сделанное любой моделью, промптом или типами.

        Каждое попадание чистится с текущими типами, так что тип, которого больше нет в
        конфиге, никогда не попадает в entity_id (он становится `Other`).

        `any_version=True` использует стадия `graph`: если модель или промпт сменили между
        стадиями `extract` и `graph`, результаты уже сделанного извлечения всё равно
        относятся к этому документу. Возвращает None, если в кэше ничего нет."""
        raw = self._registry.get_extraction(chunk.chunk_id, self._version, self.model)
        if raw is None and any_version:
            raw = self._registry.get_latest_extraction(chunk.chunk_id)
        if raw is None:
            return None
        return clean_extraction(ExtractionResult.model_validate_json(raw), self._types)

    async def extract(self, chunk: ChunkRecord, title: str) -> ExtractionResult:
        """Извлечение одного чанка: из кэша или новым вызовом LLM.

        `title` — заголовок документа, даётся модели как контекст вместе с заголовками
        разделов чанка. Побочные эффекты: вызов LLM (structured output по схеме
        `ExtractionResult`) и запись очищенного результата в `extraction_cache`.
        Исключения: ошибки LLM-клиента и валидации ответа пробрасываются."""
        hit = self.cached(chunk)
        if hit is not None:
            return hit
        messages = render_messages(
            "extract",
            entity_types=format_entity_types(self._types),
            title=title,
            headings=" > ".join(chunk.headings) or "-",
            text=chunk.text,
        )
        raw = await self._llm.structured(messages, ExtractionResult, task="extract")
        result = clean_extraction(raw, self._types)
        self._registry.put_extraction(
            chunk.chunk_id, self._version, self.model, result.model_dump_json()
        )
        return result

    async def extract_many(
        self, chunks: list[ChunkRecord], title: str, progress: ProgressSink = NULL_PROGRESS
    ) -> dict[str, ExtractionResult]:
        """Извлекает все чанки параллельно; `progress` продвигается по завершении каждого
        (включая попадания в кэш и ошибки).

        Параллелизм ограничивает семафор LLM-клиента, здесь все задачи запускаются сразу.
        Ошибка одного чанка логируется (`extract_failed`) и не прерывает остальные.
        Возвращает `{chunk_id: результат}` только для успешных чанков; долю неудачных
        проверяет вызывающий код (`extract.max_failed_ratio`)."""

        async def one(chunk: ChunkRecord) -> tuple[str, ExtractionResult | None]:
            try:
                result = await self.extract(chunk, title)
            except Exception as exc:  # один плохой чанк не должен ронять документ
                self._log.warning(
                    "extract_failed",
                    chunk_id=chunk.chunk_id,
                    error_type=type(exc).__name__,
                    error=safe_error(exc),
                )
                progress.advance(failed=True)
                return chunk.chunk_id, None
            progress.advance()
            return chunk.chunk_id, result

        pairs = await asyncio.gather(*(one(c) for c in chunks))
        return {cid: r for cid, r in pairs if r is not None}
