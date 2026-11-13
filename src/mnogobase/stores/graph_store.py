"""Графовое хранилище mnogobase в Neo4j: весь Cypher проекта живёт здесь.

Модель графа:

- `(:Document {doc_id, path, title, mime, n_pages, ingested_at})` — исходный файл;
  `doc_id = sha256(байты файла)[:16]`, то есть узел описывает содержимое, а `path` — текущий
  путь к одной из его копий (меняется `set_document_path`).
- `(:Chunk {chunk_id, doc_id, idx, text, headings, page_start, page_end, n_tokens, modality})`
  — чанк документа; `chunk_id = "<doc_id>:<idx:05d>"`. `context_text` и путь в графе не
  хранятся (путь берётся из Document).
- `(:Entity {entity_id, name, type, aliases, aliases_text, description, descriptions,
  mention_count})` — сущность; `entity_id` от типа и нормализованного имени. После
  `graph analyze` у неё появляются `analytics_run_id`, `analytics_pagerank`,
  `analytics_community`.
- `(:WikiPage {page_id, slug, title, path, content_hash, version, stale, updated_at})` —
  сгенерированная wiki-страница; `page_id == entity_id` сущности, которой она посвящена.
- `(:AnalysisState {key: 'graph', revision, run_id, metadata, completed_at, publications})` —
  единственный узел состояния офлайн-аналитики GDS.

Связи:

- `(Document)-[:HAS_CHUNK]->(Chunk)` и `(Chunk)-[:NEXT]->(Chunk)` — порядок чанков.
- `(Chunk)-[:MENTIONS]->(Entity)` — чанк упоминает сущность; `Entity.mention_count` — число
  таких чанков, пересчитывается при каждом изменении.
- `(Entity)-[:RELATED {predicate, description, evidence, strengths, weight}]->(Entity)` —
  извлечённая связь; одна на тройку (источник, предикат, цель). `evidence` — список
  `chunk_id`, которые её подтверждают, `strengths` — параллельный список сил (1..10) от тех
  же чанков, `weight` — их сумма (вес для обхода и GDS). Связь без подтверждений удаляется.
- `(WikiPage)-[:ABOUT]->(Entity)`, `(WikiPage)-[:LINKS_TO]->(WikiPage)` (ссылки `[[...]]`),
  `(WikiPage)-[:CITES]->(Chunk)` (сноски Sources).

Схема (`_SCHEMA`) — уникальность `doc_id`, `chunk_id`, `entity_id`, `page_id` и
`AnalysisState.key`, индексы `WikiPage.slug`, `Chunk.doc_id`, составной индекс сообществ
сущностей и полнотекстовый индекс `entity_names` по имени и алиасам. Все команды
`IF NOT EXISTS`; `ensure_schema` применяет их при каждом старте.

Правила транзакций:

- Все записи — идемпотентные `MERGE` по детерминированным id, повтор стадии безопасен.
- Любое изменение исходных данных (документы, чанки, сущности, связи) в той же транзакции
  меняет `AnalysisState.revision` и сбрасывает `run_id` (`_INVALIDATE_ANALYSIS`), так что
  устаревшие оценки PageRank/Leiden не используются. Этот `MERGE` стоит первым и берёт
  блокировку записи на узел состояния: публикация анализа (`publish_analysis`) берёт её же и
  поэтому сериализуется с записями.
- Многошаговые изменения (`add_chunk_facts`, `upsert_wiki_page`, удаление документа,
  публикация анализа) — одна write-транзакция: сбой посередине не оставит граф в
  промежуточном состоянии.
- Чтения (поиск, обход, контекст сущности) блокировок не берут.

Удаление документа (`_delete_document_tx`, одна транзакция): из `evidence`/`strengths` связей,
которые подтверждал документ, убираются его чанки (описание такой связи очищается, так как
его происхождение больше не известно; связь без подтверждений удаляется), затем удаляются чанки и Document, после чего
у затронутых сущностей пересчитывается `mention_count`, и сущности без упоминаний удаляются
вместе со своими wiki-страницами. `document_deletion_plan` заранее, только чтением, считает
то же самое. Новые данные, привязанные к документу, нужно учитывать в обеих функциях.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from neo4j import (
    Driver,
    GraphDatabase,
    ManagedTransaction,
    NotificationDisabledClassification,
    Query,
)
from neo4j.exceptions import ClientError, Neo4jError

from mnogobase.core.config import Neo4jSettings
from mnogobase.core.log import get_logger
from mnogobase.core.models import (
    ChunkRecord,
    ChunkView,
    DocumentRecord,
    EntityContext,
    EntityRecord,
    PageIndexRow,
    RelationView,
    WikiPageRecord,
)

# Конфиг драйвера для всех драйверов GraphStore. Уведомления UNRECOGNIZED ("relationship
# type X does not exist") нормальны для свежей базы, иначе они логировались бы как WARNING.
DRIVER_OPTIONS: dict[str, object] = {
    "notifications_disabled_classifications": [NotificationDisabledClassification.UNRECOGNIZED],
}

# Ограничения и индексы графа; применяются на каждом старте (`ensure_schema`).
_SCHEMA = [
    "CREATE CONSTRAINT document_id IF NOT EXISTS FOR (d:Document) REQUIRE d.doc_id IS UNIQUE",
    "CREATE CONSTRAINT chunk_id IF NOT EXISTS FOR (c:Chunk) REQUIRE c.chunk_id IS UNIQUE",
    "CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (e:Entity) REQUIRE e.entity_id IS UNIQUE",
    "CREATE CONSTRAINT page_id IF NOT EXISTS FOR (p:WikiPage) REQUIRE p.page_id IS UNIQUE",
    "CREATE INDEX page_slug IF NOT EXISTS FOR (p:WikiPage) ON (p.slug)",
    "CREATE INDEX chunk_doc IF NOT EXISTS FOR (c:Chunk) ON (c.doc_id)",
    "CREATE FULLTEXT INDEX entity_names IF NOT EXISTS FOR (e:Entity) ON EACH [e.name, e.aliases_text]",
    "CREATE CONSTRAINT analysis_state_key IF NOT EXISTS FOR (s:AnalysisState) REQUIRE s.key IS UNIQUE",
    "CREATE INDEX entity_community IF NOT EXISTS FOR (e:Entity) "
    "ON (e.analytics_run_id, e.analytics_community)",
]
# Слово для полнотекстового запроса: буквы/цифры любого алфавита, без операторов Lucene.
_WORD = re.compile(r"\w+", re.UNICODE)
# Каждое слово запроса — клауза по двум полям индекса `entity_names`; без предела вопрос
# в несколько сотен слов превысил бы лимит клауз Lucene (1024) и уронил бы поиск по графу.
FULLTEXT_MAX_TOKENS = 64
# Проекция узла WikiPage в `WikiPageRecord`; отсутствующий `stale` считается false.
_PAGE_FIELDS = "p {.page_id, .slug, .title, .path, .content_hash, .version, stale: coalesce(p.stale, false)} AS page"
# Проекция связи RELATED в `RelationView` (концы, предикат, описание, вес, подтверждения).
_REL_MAP = (
    "{src_id: startNode(r).entity_id, src_name: startNode(r).name, predicate: r.predicate, "
    "dst_id: endNode(r).entity_id, dst_name: endNode(r).name, description: r.description, "
    "weight: r.weight, evidence: r.evidence}"
)
# Помечает аналитику устаревшей: новая ревизия графа, опубликованного запуска больше нет.
# Выполняется первым в каждой транзакции, которая меняет исходные данные графа.
_INVALIDATE_ANALYSIS = (
    "MERGE (s:AnalysisState {key: 'graph'}) SET s.revision = randomUUID(), s.run_id = null"
)


# Имена GDS-проекций, которые создаёт `graph analyze`; чужие проекции не трогаем.
PROJECTION_PREFIX = "mb_analysis_"


@dataclass
class DeleteResult:
    """Итог (или план) удаления документа из графа.

    `affected` — id всех затронутых сущностей (упомянутых документом и переданных
    `extra_entity_ids`); `removed_entity_ids` / `removed_names` — те, что удаляются целиком,
    потому что вне документа их больше никто не упоминает; `removed_pages` — пары
    `(page_id, path)` wiki-страниц этих сущностей, файлы которых нужно удалить."""

    affected: list[str]
    removed_entity_ids: list[str]
    removed_names: list[str]
    removed_pages: list[tuple[str, str]]


class GraphStore:
    """Доступ к графу mnogobase в Neo4j через один `neo4j.Driver`.

    Методы синхронные; каждый открывает свою сессию или транзакцию. Драйвер создаётся
    снаружи (`from_settings` или тестовый контейнер), `close` его закрывает."""

    def __init__(self, driver: Driver):
        self._driver = driver

    @classmethod
    def from_settings(cls, settings: Neo4jSettings) -> GraphStore:
        """Хранилище с драйвером из секции `neo4j` (URI, пользователь, пароль из `.env`).

        Соединение открывается лениво, при первом запросе; проверить его — `verify`."""
        driver = GraphDatabase.driver(
            settings.uri, auth=(settings.user, settings.password()), **DRIVER_OPTIONS
        )
        return cls(driver)

    def close(self) -> None:
        """Закрывает драйвер и его пул соединений."""
        self._driver.close()

    def verify(self) -> None:
        """Проверяет доступность Neo4j (для `doctor` и preflight); бросает ошибку драйвера."""
        self._driver.verify_connectivity()

    def _run(self, query: str, *, _timeout: float | None = None, **params) -> list[dict]:
        """Выполняет запрос в собственной автоматической транзакции и возвращает строки как
        словари. `_timeout` (секунды) — серверный таймаут транзакции; при превышении Neo4j
        бросает ошибку с кодом `...TransactionTimedOut`. Аналитику сам не сбрасывает:
        исходные данные графа пишутся через `_write`."""
        statement = Query(query, timeout=_timeout) if _timeout is not None else query
        records, _, _ = self._driver.execute_query(statement, parameters_=params)
        return [r.data() for r in records]

    def _write(self, query: str, **params) -> None:
        """Выполняет запрос записи исходных данных в одной транзакции с
        `_INVALIDATE_ANALYSIS`: оценки GDS устаревают атомарно с изменением графа. Узел
        состояния блокируется первым, чтобы порядок блокировок совпадал с
        `publish_analysis`."""

        def write(tx):
            tx.run(_INVALIDATE_ANALYSIS, **params).consume()
            tx.run(query, **params).consume()

        with self._driver.session() as session:
            session.execute_write(write)

    # ---- схема / обслуживание ----
    def ensure_schema(self) -> None:
        """Применяет `_SCHEMA`, создаёт узел `AnalysisState` (если его нет) и ждёт до 60 с,
        пока индексы станут ONLINE: полнотекстовый поиск по свежему индексу иначе ничего не
        нашёл бы."""
        for statement in _SCHEMA:
            self._run(statement)
        self._run("MERGE (s:AnalysisState {key: 'graph'}) ON CREATE SET s.revision = randomUUID()")
        self._run("CALL db.awaitIndexes(60)")

    def wipe(self) -> None:
        """Удаляет узлы mnogobase пачками по своим транзакциям: одна транзакция на весь
        большой граф не помещается в heap Neo4j.

        Удаляются только метки mnogobase (Document, Chunk, Entity, WikiPage,
        AnalysisState) со всеми связями; схема остаётся. Для `reset`. Неатомарно: прерванный
        wipe оставит часть узлов, повтор их доудалит."""
        with self._driver.session() as session:
            session.run(
                "MATCH (n) WHERE n:Document OR n:Chunk OR n:Entity OR n:WikiPage "
                "OR n:AnalysisState "
                "CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 1000 ROWS"
            ).consume()

    def counts(self) -> dict[str, int]:
        """Число узлов каждой метки и связей RELATED (для `status`)."""
        out = {
            label: self._run(f"MATCH (n:{label}) RETURN count(n) AS n")[0]["n"]
            for label in ("Document", "Chunk", "Entity", "WikiPage")
        }
        out["RELATED"] = self._run("MATCH (:Entity)-[r:RELATED]->(:Entity) RETURN count(r) AS n")[
            0
        ]["n"]
        return out

    # ---- документы / чанки ----
    def upsert_document(self, doc: DocumentRecord) -> None:
        """Создаёт или обновляет узел Document; `ingested_at` — время этой записи."""
        self._write(
            "MERGE (d:Document {doc_id: $doc_id}) "
            "SET d.path = $path, d.title = $title, d.mime = $mime, d.n_pages = $n_pages, "
            "d.ingested_at = datetime()",
            **doc.model_dump(),
        )

    def upsert_chunks(self, chunks: list[ChunkRecord]) -> None:
        """Записывает чанки документа и связи HAS_CHUNK и NEXT.

        Document должен уже существовать: строки без него молча пропускаются (`MATCH`).
        `chunks` — все чанки одного документа по порядку, NEXT связывает соседние. Две
        отдельные транзакции: чанки, затем NEXT (повтор достраивает недостающее)."""
        rows = [c.model_dump(exclude={"context_text", "path"}) for c in chunks]
        # для каждой строки: найти документ, MERGE чанк по chunk_id, переписать его свойства
        # (`+=` с проекцией — только перечисленные поля), MERGE ребро HAS_CHUNK
        self._write(
            "UNWIND $rows AS row "
            "MATCH (d:Document {doc_id: row.doc_id}) "
            "MERGE (c:Chunk {chunk_id: row.chunk_id}) "
            "SET c += row {.doc_id, .idx, .text, .headings, .page_start, .page_end, .n_tokens, .modality} "
            "MERGE (d)-[:HAS_CHUNK]->(c)",
            rows=rows,
        )
        pairs = [[a.chunk_id, b.chunk_id] for a, b in pairwise(chunks)]
        if pairs:
            self._write(
                "UNWIND $pairs AS p "
                "MATCH (a:Chunk {chunk_id: p[0]}), (b:Chunk {chunk_id: p[1]}) "
                "MERGE (a)-[:NEXT]->(b)",
                pairs=pairs,
            )

    def document_path(self, doc_id: str) -> str | None:
        """Путь, записанный в узле Document (None, если документ неизвестен)."""
        rows = self._run("MATCH (d:Document {doc_id: $id}) RETURN d.path AS path", id=doc_id)
        return rows[0]["path"] if rows else None

    def set_document_path(self, doc_id: str, path: str) -> None:
        """Переносит Document на другой путь (переименование или удаление одной из копий)."""
        self._write("MATCH (d:Document {doc_id: $id}) SET d.path = $path", id=doc_id, path=path)

    def pages_citing_document(self, doc_id: str) -> list[str]:
        """Wiki-страницы (page_id == entity_id), чьи Sources цитируют чанк этого документа.

        Находит и страницы, цитирующие чанки, у которых MENTIONS не записаны (упавшая стадия
        graph), поэтому удаление документа пересобирает и их."""
        rows = self._run(
            "MATCH (p:WikiPage)-[:CITES]->(:Chunk {doc_id: $id}) RETURN DISTINCT p.page_id AS pid",
            id=doc_id,
        )
        return sorted(r["pid"] for r in rows)

    def has_document(self, doc_id: str) -> bool:
        """Есть ли в графе узел Document с этим id."""
        return bool(self._run("MATCH (d:Document {doc_id: $id}) RETURN 1 AS x LIMIT 1", id=doc_id))

    def document_deletion_plan(
        self, doc_id: str, extra_entity_ids: Sequence[str] = ()
    ) -> DeleteResult:
        """Только чтение: что удалил бы `delete_document(doc_id, extra_entity_ids)` (пусто,
        если документ неизвестен). Затронуты сущности, которые упоминают чанки документа, и
        `extra_entity_ids` (записанные стадией graph документа, даже без MENTIONS). Сущность
        удаляется, если её не упоминает ни один чанк вне этого документа."""
        # 1) подзапрос собирает затронутые сущности: упомянутые чанками документа и `$extra`;
        # 2) для каждой — её wiki-страницы (ABOUT) как пары [page_id, path];
        # 3) `others` — сколько чанков ВНЕ документа её упоминают; 0 означает, что сущность
        #    удаляется вместе со страницами
        rows = self._run(
            "CALL () { "
            "  MATCH (:Document {doc_id: $doc_id})-[:HAS_CHUNK]->(:Chunk)-[:MENTIONS]->(e:Entity) "
            "  RETURN e "
            "  UNION "
            "  MATCH (e:Entity) WHERE e.entity_id IN $extra RETURN e "
            "} "
            "WITH DISTINCT e "
            "OPTIONAL MATCH (p:WikiPage)-[:ABOUT]->(e) "
            "WITH e, collect(p) AS pages "
            "RETURN e.entity_id AS eid, e.name AS name, "
            "[x IN pages | [x.page_id, x.path]] AS page_refs, "
            "COUNT { (e)<-[:MENTIONS]-(x:Chunk) "
            "WHERE NOT (:Document {doc_id: $doc_id})-[:HAS_CHUNK]->(x) } AS others",
            doc_id=doc_id,
            extra=list(extra_entity_ids),
        )
        removed = [r for r in rows if r["others"] == 0]
        return DeleteResult(
            affected=[r["eid"] for r in rows],
            removed_entity_ids=[r["eid"] for r in removed],
            removed_names=[r["name"] for r in removed],
            removed_pages=[(p[0], p[1]) for r in removed for p in r["page_refs"]],
        )

    def delete_document(self, doc_id: str, extra_entity_ids: Sequence[str] = ()) -> DeleteResult:
        """Удаляет документ из графа и всё, что держалось только на нём (см. описание модуля
        и `_delete_document_tx`).

        `extra_entity_ids` — сущности, которые стадия graph записала для документа (реестр
        `doc_entities`), даже если MENTIONS к ним не успели появиться. Идемпотентно: для уже
        удалённого документа только пересчитывает `extra_entity_ids`. Возвращает
        `DeleteResult` с фактически удалёнными сущностями и страницами."""
        # одна write-транзакция: падение посреди каскада не должно оставить Document удалённым,
        # а его сущности — с устаревшими счётчиками упоминаний / осиротевшими wiki-страницами
        # (повтор ничего бы не сделал)
        with self._driver.session() as session:
            return session.execute_write(_delete_document_tx, doc_id, list(extra_entity_ids))

    def chunks_by_ids(self, chunk_ids: list[str]) -> list[ChunkView]:
        """Чанки по id вместе с путём их документа (для цитат); несуществующие пропускаются.
        Порядок результата не гарантирован."""
        rows = self._run(
            "UNWIND $ids AS cid "
            "MATCH (d:Document)-[:HAS_CHUNK]->(c:Chunk {chunk_id: cid}) "
            "RETURN c.chunk_id AS chunk_id, c.doc_id AS doc_id, c.text AS text, d.path AS path, "
            "c.page_start AS page, coalesce(c.headings, []) AS headings",
            ids=chunk_ids,
        )
        return [ChunkView(**r) for r in rows]

    def chunks_mentioning(self, entity_id: str, chunk_ids: list[str]) -> list[ChunkView]:
        """Те из `chunk_ids`, что ещё существуют и всё ещё упоминают сущность, в порядке входа.

        Нужен wiki builder: прежние доказательства страницы перепроверяются, и чанки
        удалённых документов или потерявшие упоминание выпадают."""
        # перебираем индексы списка, чтобы сохранить входной порядок через ORDER BY i
        rows = self._run(
            "UNWIND range(0, size($ids) - 1) AS i "
            "MATCH (d:Document)-[:HAS_CHUNK]->(c:Chunk {chunk_id: $ids[i]})"
            "-[:MENTIONS]->(:Entity {entity_id: $eid}) "
            "RETURN c.chunk_id AS chunk_id, c.doc_id AS doc_id, c.text AS text, d.path AS path, "
            "c.page_start AS page, coalesce(c.headings, []) AS headings ORDER BY i",
            ids=chunk_ids,
            eid=entity_id,
        )
        return [ChunkView(**r) for r in rows]

    def doc_chunks(self, doc_id: str) -> tuple[DocumentRecord, list[ChunkRecord]] | None:
        """Документ и его чанки в том виде, как они хранятся в графе (None, если документ
        неизвестен).

        `context_text` не хранится; он собирается заново как заголовки + текст, по одному на
        строку, как в `contextualize` чанкера (прочие метаданные чанка, которые тот может
        добавить, например подписи, теряются)."""
        rows = self._run(
            "MATCH (d:Document {doc_id: $doc_id}) "
            "OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:Chunk) "
            "WITH d, c ORDER BY c.idx "
            "RETURN d {.doc_id, .path, .title, .mime, .n_pages} AS doc, collect(c {.*}) AS chunks",
            doc_id=doc_id,
        )
        if not rows:
            return None
        doc = DocumentRecord(**rows[0]["doc"])
        chunks = [
            ChunkRecord(
                **c,
                context_text="\n".join([*(c.get("headings") or []), c["text"]]),
                path=doc.path,
            )
            for c in rows[0]["chunks"]
        ]
        return doc, chunks

    def count_doc_chunks(self, doc_id: str) -> int:
        """Число чанков документа (0, если документа нет)."""
        rows = self._run("MATCH (c:Chunk {doc_id: $doc_id}) RETURN count(c) AS n", doc_id=doc_id)
        return rows[0]["n"] if rows else 0

    def chunk_entity_ids(self, doc_id: str) -> dict[str, list[str]]:
        """`{chunk_id: [entity_id, ...]}` для всех чанков документа (у чанка без упоминаний —
        пустой список). Используется, чтобы восстановить `entity_ids` в Qdrant при `reindex`."""
        rows = self._run(
            "MATCH (c:Chunk {doc_id: $doc_id}) "
            "OPTIONAL MATCH (c)-[:MENTIONS]->(e:Entity) "
            "RETURN c.chunk_id AS cid, collect(e.entity_id) AS eids",
            doc_id=doc_id,
        )
        return {r["cid"]: r["eids"] for r in rows}

    # ---- сущности / связи ----
    def get_entity(self, entity_id: str) -> EntityRecord | None:
        """Сущность по id или None."""
        rows = self._run("MATCH (e:Entity {entity_id: $id}) RETURN e {.*} AS e", id=entity_id)
        return EntityRecord(**rows[0]["e"]) if rows else None

    def get_entities(self, entity_ids: list[str]) -> list[EntityRecord]:
        """Существующие сущности из `entity_ids` (отсутствующие пропускаются)."""
        rows = self._run(
            "UNWIND $ids AS id MATCH (e:Entity {entity_id: id}) RETURN e {.*} AS e",
            ids=entity_ids,
        )
        return [EntityRecord(**r["e"]) for r in rows]

    def upsert_entity(self, entity: EntityRecord) -> None:
        """То же, что `upsert_entities([entity])`."""
        self.upsert_entities([entity])

    def upsert_entities(self, entities: list[EntityRecord]) -> None:
        """Создаёт или обновляет узлы Entity.

        Перезаписываются имя, тип, алиасы, описания; `mention_count` сохраняется (новой
        сущности ставится 0). `aliases_text` — алиасы одной строкой через ` | ` для
        полнотекстового индекса `entity_names` (он индексирует строки, а не списки)."""
        if not entities:
            return
        self._write(
            "UNWIND $rows AS row MERGE (e:Entity {entity_id: row.entity_id}) "
            "SET e.name = row.name, e.type = row.type, e.aliases = row.aliases, "
            "e.aliases_text = row.aliases_text, e.description = row.description, "
            "e.descriptions = row.descriptions, e.mention_count = coalesce(e.mention_count, 0)",
            rows=[
                {
                    "entity_id": e.entity_id,
                    "name": e.name,
                    "type": e.type,
                    "aliases": e.aliases,
                    "aliases_text": " | ".join(e.aliases),
                    "description": e.description,
                    "descriptions": e.descriptions,
                }
                for e in entities
            ],
        )

    def entities(self, min_mentions: int = 0) -> list[EntityRecord]:
        """Все сущности с `mention_count >= min_mentions`, по имени."""
        rows = self._run(
            "MATCH (e:Entity) WHERE e.mention_count >= $m RETURN e {.*} AS e ORDER BY e.name",
            m=min_mentions,
        )
        return [EntityRecord(**r["e"]) for r in rows]

    def add_chunk_facts(self, chunk_id: str, entity_ids: list[str], relations: list[dict]) -> None:
        """Упоминания и связи одного чанка одной транзакцией (UNWIND вместо транзакции на
        каждое упоминание). `relations`: dict с src, dst, predicate, description, strength.
        Связь, уже подтверждённая этим чанком, не меняется, поэтому повтор идемпотентен;
        повтор одной связи внутри чанка учитывается один раз (первый).

        Чанк и сущности должны уже существовать (иначе строки молча пропускаются `MATCH`).
        Вызывается после `QdrantStore.set_chunk_entities` (порядок записи: Qdrant, затем
        Neo4j)."""
        unique: dict[tuple[str, str, str], dict] = {}
        for rel in relations:
            unique.setdefault((rel["src"], rel["dst"], rel["predicate"]), rel)
        if not entity_ids and not unique:
            return

        def write(tx):
            tx.run(_INVALIDATE_ANALYSIS).consume()
            if entity_ids:
                # MERGE ребро MENTIONS к каждой сущности и пересчитать её mention_count по
                # фактическому числу упоминающих чанков (а не +1: повтор не задвоит счёт)
                tx.run(
                    "MATCH (c:Chunk {chunk_id: $chunk_id}) "
                    "UNWIND $eids AS eid "
                    "MATCH (e:Entity {entity_id: eid}) "
                    "MERGE (c)-[:MENTIONS]->(e) "
                    "WITH DISTINCT e "
                    "SET e.mention_count = COUNT { (e)<-[:MENTIONS]-(:Chunk) }",
                    chunk_id=chunk_id,
                    eids=entity_ids,
                ).consume()
            if unique:
                # для каждой связи: MERGE ребро RELATED по (src, predicate, dst); у новой —
                # пустые списки и вес 0. Если этот чанк уже есть в evidence, строка
                # отсеивается (идемпотентность). Иначе chunk_id и сила дописываются в
                # evidence/strengths, вес растёт на силу, а описание заменяется более длинным.
                tx.run(
                    "UNWIND $rels AS rel "
                    "MATCH (a:Entity {entity_id: rel.src}), (b:Entity {entity_id: rel.dst}) "
                    "MERGE (a)-[r:RELATED {predicate: rel.predicate}]->(b) "
                    "ON CREATE SET r.evidence = [], r.strengths = [], r.weight = 0.0, "
                    "r.description = rel.description "
                    "WITH r, rel WHERE NOT $chunk_id IN r.evidence "
                    "SET r.evidence = r.evidence + $chunk_id, "
                    "r.strengths = r.strengths + rel.strength, r.weight = r.weight + rel.strength, "
                    "r.description = CASE WHEN size(rel.description) > "
                    "size(coalesce(r.description, '')) THEN rel.description ELSE r.description END",
                    chunk_id=chunk_id,
                    rels=list(unique.values()),
                ).consume()

        with self._driver.session() as session:
            session.execute_write(write)

    def entity_context(self, entity_id: str, max_relations: int) -> EntityContext:
        """Сущность и до `max_relations` её связей RELATED (в обе стороны), самые тяжёлые
        первыми; для генерации wiki-страницы. Исключения: `KeyError`, если сущности нет."""
        rows = self._run(
            "MATCH (e:Entity {entity_id: $id}) "
            "OPTIONAL MATCH (e)-[r:RELATED]-(:Entity) "
            "WITH e, r ORDER BY r.weight DESC "
            "WITH e, collect(r)[..$limit] AS rels "
            f"RETURN e {{.*}} AS entity, [r IN rels | {_REL_MAP}] AS relations",
            id=entity_id,
            limit=max_relations,
        )
        if not rows:
            raise KeyError(entity_id)
        return EntityContext(
            entity=EntityRecord(**rows[0]["entity"]),
            relations=[RelationView(**r) for r in rows[0]["relations"]],
        )

    def fulltext_entities(self, query: str, k: int) -> list[tuple[str, float]]:
        """До `k` сущностей, чьё имя или алиасы совпадают со словами запроса, по индексу
        `entity_names` (Lucene, слова объединяются через OR). Возвращает пары
        `(entity_id, score)` в порядке убывания score. Начальные узлы режима `graph`."""
        # простые словесные токены в нижнем регистре: операторы Lucene и спецсимволы не проходят
        tokens = list(dict.fromkeys(t.casefold() for t in _WORD.findall(query)))
        tokens = tokens[:FULLTEXT_MAX_TOKENS]
        if not tokens:
            return []
        rows = self._run(
            "CALL db.index.fulltext.queryNodes('entity_names', $q) YIELD node, score "
            "RETURN node.entity_id AS id, score LIMIT $k",
            q=" ".join(tokens),
            k=k,
        )
        return [(r["id"], r["score"]) for r in rows]

    def neighborhood(self, seed_ids: list[str], hops: int, limit: int) -> list[RelationView]:
        """Связи RELATED в пределах `hops` (1..3) шагов от начальных сущностей, на чистом
        Cypher (без APOC).

        Сортировка: по весу, затем по суммарной степени концов (хорошо связанные узлы
        полезнее как контекст). Число путей ограничено 5000, чтобы узел-хаб не взорвал
        перебор."""
        hops = max(1, min(int(hops), 3))  # границы переменной длины не могут быть параметрами
        # 1) все пути длиной 1..hops по RELATED от начальных узлов (только через Entity),
        #    не больше 5000; 2) связи этих путей без повторов; 3) степень каждого конца —
        #    число его соседей с метками mnogobase; 4) сортировка и обрезка до $limit
        rows = self._run(
            "MATCH (s:Entity) WHERE s.entity_id IN $seeds "
            f"MATCH p = (s)-[:RELATED*1..{hops}]-(:Entity) "
            "WHERE all(n IN nodes(p) WHERE n:Entity) "
            "WITH p LIMIT 5000 "
            "UNWIND relationships(p) AS r "
            "WITH DISTINCT r "
            "WITH r, startNode(r) AS a, endNode(r) AS b "
            "WITH r, a, b, "
            "COUNT { (a)--(n) WHERE n:Document OR n:Chunk OR n:Entity OR n:WikiPage } + "
            "COUNT { (b)--(n) WHERE n:Document OR n:Chunk OR n:Entity OR n:WikiPage } AS degree "
            "RETURN a.entity_id AS src_id, a.name AS src_name, r.predicate AS predicate, "
            "b.entity_id AS dst_id, b.name AS dst_name, r.description AS description, "
            "r.weight AS weight, r.evidence AS evidence, degree "
            "ORDER BY weight DESC, degree DESC LIMIT $limit",
            seeds=seed_ids,
            limit=limit,
        )
        return [RelationView(**r) for r in rows]

    def neighborhood_apoc(
        self,
        seed_ids: list[str],
        hops: int,
        limit: int,
        *,
        max_nodes: int = 20_000,
        timeout: float = 5.0,
    ) -> list[RelationView]:
        """То же, что `neighborhood`, но обход — ограниченный BFS APOC (`graph.traversal`).

        Откаты на `neighborhood`: сущностей больше `max_nodes` (список разрешённых узлов
        передаётся целиком и был бы слишком большим) или APOC не установлен; при таймауте
        `timeout` секунд — `neighborhood` в один шаг. Остальные ошибки Neo4j
        пробрасываются. В отличие от `neighborhood`, возвращает все связи между
        достигнутыми узлами, а не только рёбра найденных путей."""
        if not seed_ids or limit <= 0:
            return []
        count = self._run("MATCH (e:Entity) RETURN count(e) AS n")[0]["n"]
        if count > max_nodes:
            get_logger(__name__).info("apoc_fallback", reason="node_budget", nodes=count)
            return self.neighborhood(seed_ids, hops, limit)
        try:
            # 1) allowed — все узлы Entity (обход не выйдет на Chunk/WikiPage);
            # 2) от каждого начального узла BFS по RELATED до $hops шагов, каждый узел
            #    посещается один раз (NODE_GLOBAL), не больше 5000 путей;
            # 3) reached — концы найденных путей (с minLevel 0 — и сами начальные узлы);
            # 4) все связи RELATED между достигнутыми узлами, по весу и затем стабильно по id
            rows = self._run(
                "MATCH (n:Entity) WITH collect(n) AS allowed "
                "UNWIND $seeds AS sid MATCH (s:Entity {entity_id: sid}) "
                "CALL apoc.path.expandConfig(s, {relationshipFilter:'RELATED', "
                "labelFilter:$label_filter, allowlistNodes:allowed, filterStartNode:true, "
                "minLevel:0, maxLevel:$hops, bfs:true, uniqueness:'NODE_GLOBAL', limit:5000}) "
                "YIELD path WITH path LIMIT 5000 "
                "WITH collect(DISTINCT last(nodes(path))) AS reached "
                "UNWIND reached AS a MATCH (a)-[r:RELATED]-(b:Entity) WHERE b IN reached "
                "WITH DISTINCT r WITH r, startNode(r) AS a, endNode(r) AS b "
                "RETURN a.entity_id AS src_id, a.name AS src_name, r.predicate AS predicate, "
                "b.entity_id AS dst_id, b.name AS dst_name, r.description AS description, "
                "r.weight AS weight, r.evidence AS evidence "
                "ORDER BY weight DESC, src_id, predicate, dst_id LIMIT $limit",
                seeds=seed_ids,
                hops=max(1, min(int(hops), 3)),
                limit=limit,
                label_filter="+Entity",
                _timeout=timeout,
            )
        except ClientError as exc:
            code = exc.code or ""
            if code == "Neo.ClientError.Procedure.ProcedureNotFound":
                get_logger(__name__).warning("apoc_fallback", reason="procedure_missing")
                return self.neighborhood(seed_ids, hops, limit)
            if "TransactionTimedOut" in code:
                # обход вокруг узла-хаба не уложился в timeout: соседи в один шаг дёшевы и
                # всё равно дают контекст, а ответ в режимах graph/all не падает целиком
                get_logger(__name__).warning("apoc_fallback", reason="timeout")
                return self.neighborhood(seed_ids, 1, limit)
            raise
        return [RelationView(**r) for r in rows]

    # ---- производная аналитика (не меняет IDs и evidence) ----

    def analysis_status(self) -> dict:
        """Состояние аналитики: `revision`, `run_id` (None — нет актуального запуска),
        `ready`, `completed_at` и `metadata` (отчёт `graph analyze`, из JSON)."""
        rows = self._run(
            "MATCH (s:AnalysisState {key: 'graph'}) "
            "RETURN s.revision AS revision, s.run_id AS run_id, s.metadata AS metadata, "
            "toString(s.completed_at) AS completed_at"
        )
        state = rows[0] if rows else {"revision": None, "run_id": None}
        metadata = json.loads(state.pop("metadata", None) or "{}")
        return {**state, "ready": bool(state["run_id"]), "metadata": metadata}

    def analysis_scores(self, entity_ids: list[str]) -> dict[str, float]:
        """PageRank сущностей из актуального запуска аналитики (`graph.use_gds`).

        Сущности без оценки этого запуска (или все, если запуск устарел) отсутствуют в
        словаре: устаревшие значения в узлах игнорируются по `analytics_run_id`."""
        rows = self._run(
            "MATCH (s:AnalysisState {key: 'graph'}) WHERE s.run_id IS NOT NULL "
            "MATCH (e:Entity) WHERE e.entity_id IN $ids AND e.analytics_run_id = s.run_id "
            "RETURN e.entity_id AS id, e.analytics_pagerank AS score",
            ids=entity_ids,
        )
        return {r["id"]: r["score"] for r in rows}

    def entity_analysis(self, entity_id: str) -> dict:
        """PageRank и сообщество сущности из актуального запуска аналитики; `None`, если
        запуска нет, он устарел или сущности нет (для карточки сущности в вебе)."""
        rows = self._run(
            "MATCH (s:AnalysisState {key: 'graph'}) WHERE s.run_id IS NOT NULL "
            "MATCH (e:Entity {entity_id: $id}) WHERE e.analytics_run_id = s.run_id "
            "RETURN e.analytics_pagerank AS pagerank, e.analytics_community AS community",
            id=entity_id,
        )
        return rows[0] if rows else {"pagerank": None, "community": None}

    def analysis_details(self, limit: int = 20) -> list[dict]:
        """Топ сущностей актуального запуска по PageRank с их сообществом (`graph status`);
        `limit` зажимается в 1..100."""
        return self._run(
            "MATCH (s:AnalysisState {key: 'graph'}) WHERE s.run_id IS NOT NULL "
            "MATCH (e:Entity) WHERE e.analytics_run_id = s.run_id "
            "RETURN e.entity_id AS entity_id, e.name AS name, "
            "e.analytics_pagerank AS pagerank, e.analytics_community AS community "
            "ORDER BY pagerank DESC, entity_id LIMIT $limit",
            limit=max(1, min(limit, 100)),
        )

    def community_chunks(
        self,
        seed_ids: list[str],
        limit: int,
        *,
        max_communities: int = 3,
        max_entities: int = 30,
        exclude_chunk_ids: Sequence[str] = (),
        timeout: float = 5.0,
    ) -> list[ChunkView]:
        """Исходные чанки из сообществ Leiden, к которым относятся начальные сущности
        (`graph.community`).

        Объём ограничен до раскрытия MENTIONS: берутся не больше `max_communities` (≤ 20)
        сообществ в порядке первых начальных узлов и в каждом не больше `max_entities` (≤ 500)
        сущностей с наибольшим PageRank. Чанки из `exclude_chunk_ids` (уже найденные)
        пропускаются; результат — до `limit` (≤ 100) чанков. Работает только при актуальном
        запуске аналитики. При таймауте `timeout` возвращает пустой список (ответ не должен
        падать из-за дополнительного контекста), прочие ошибки пробрасываются."""
        if not seed_ids or limit <= 0 or max_communities <= 0 or max_entities <= 0:
            return []
        try:
            # 1) только при актуальном запуске (run_id не null);
            # 2) сообщества начальных узлов этого запуска, priority — позиция первого узла
            #    сообщества в $seeds; берём первые $groups сообществ;
            # 3) в каждом — $members сущностей с наибольшим PageRank;
            # 4) чанки, упоминающие этих сущностей (кроме $excluded), по каждому чанку —
            #    лучший приоритет, сколько сущностей упоминает и наибольший PageRank;
            # 5) сортировка: приоритет сообщества, затем чанки с большим числом упоминаний
            rows = self._run(
                "MATCH (s:AnalysisState {key:'graph'}) WHERE s.run_id IS NOT NULL "
                "UNWIND range(0, size($seeds)-1) AS priority "
                "MATCH (seed:Entity {entity_id:$seeds[priority]}) "
                "WHERE seed.analytics_run_id = s.run_id "
                "AND seed.analytics_community IS NOT NULL "
                "WITH s, seed.analytics_community AS community, min(priority) AS priority "
                "ORDER BY priority, community LIMIT $groups "
                "CALL (s, community) { "
                "  MATCH (e:Entity) WHERE e.analytics_run_id = s.run_id "
                "  AND e.analytics_community = community "
                "  RETURN e ORDER BY e.analytics_pagerank DESC, e.entity_id LIMIT $members "
                "} "
                "MATCH (d:Document)-[:HAS_CHUNK]->(c:Chunk)-[:MENTIONS]->(e) "
                "WHERE NOT c.chunk_id IN $excluded "
                "WITH d, c, min(priority) AS priority, count(DISTINCT e) AS mentions, "
                "max(e.analytics_pagerank) AS rank "
                "RETURN c.chunk_id AS chunk_id, c.doc_id AS doc_id, c.text AS text, "
                "d.path AS path, c.page_start AS page, coalesce(c.headings,[]) AS headings "
                "ORDER BY priority, mentions DESC, rank DESC, chunk_id LIMIT $limit",
                seeds=list(dict.fromkeys(seed_ids)),
                groups=min(max_communities, 20),
                members=min(max_entities, 500),
                excluded=list(exclude_chunk_ids),
                limit=min(limit, 100),
                _timeout=timeout,
            )
        except Neo4jError as exc:
            if "TransactionTimedOut" not in (exc.code or ""):
                raise
            get_logger(__name__).warning("leiden_context_timeout")
            return []
        return [ChunkView(**row) for row in rows]

    def plugin_status(self) -> dict:
        """Версии APOC и GDS (None, если плагина нет) и список недостающих процедур и
        функций, которые использует mnogobase (для `doctor` и `graph analyze`)."""
        procedures = {
            "apoc.path.expandConfig",
            "gds.graph.project",
            "gds.graph.project.estimate",
            "gds.graph.list",
            "gds.graph.drop",
            "gds.pageRank.stream",
            "gds.pageRank.stream.estimate",
            "gds.leiden.stream",
            "gds.leiden.stream.estimate",
        }
        available = {
            r["name"]
            for r in self._run(
                "SHOW PROCEDURES YIELD name WHERE name IN $names RETURN name",
                names=sorted(procedures),
            )
        }
        functions = {
            r["name"]
            for r in self._run(
                "SHOW FUNCTIONS YIELD name WHERE name IN $names RETURN name",
                names=["apoc.version", "gds.version", "gds.util.asNode"],
            )
        }
        return {
            "apoc": self._run("RETURN apoc.version() AS version")[0]["version"]
            if "apoc.version" in functions
            else None,
            "gds": self._run("RETURN gds.version() AS version")[0]["version"]
            if "gds.version" in functions
            else None,
            "missing": sorted((procedures - available) | ({"gds.util.asNode"} - functions)),
        }

    def analysis_counts(self) -> dict:
        """Размер графа для GDS: `nodes`, `relationships` и `invalid_weights` — связи с весом
        не в (0, max float], на которых взвешенные алгоритмы GDS дали бы неверный результат.
        Сравнение с максимальным double отсеивает и NaN, и бесконечность."""
        nodes = self._run("MATCH (e:Entity) RETURN count(e) AS n")[0]["n"]
        relationships = self._run(
            "MATCH (:Entity)-[r:RELATED]->(:Entity) "
            "RETURN count(r) AS relationships, sum(CASE WHEN r.weight > 0 "
            "AND r.weight <= 1.7976931348623157e308 THEN 0 ELSE 1 END) AS invalid_weights"
        )[0]
        return {"nodes": nodes, **relationships}

    def gds_project_estimate(self, timeout: float) -> int:
        """Оценка памяти (байты, верхняя граница) для проекции Entity + RELATED в GDS."""
        return int(
            self._run(
                "CALL gds.graph.project.estimate('Entity', "
                "{RELATED:{orientation:'UNDIRECTED', properties:'weight'}}, "
                "{readConcurrency:1, validateRelationships:false}) "
                "YIELD bytesMax RETURN bytesMax AS bytes",
                _timeout=timeout,
            )[0]["bytes"]
        )

    def gds_project(self, name: str, timeout: float) -> None:
        """Создаёт в GDS проекцию `name`: узлы Entity, связи RELATED как неориентированные с
        весом `weight`. Проекция живёт в памяти Neo4j до `drop_projection`."""
        self._run(
            "CALL gds.graph.project($name, 'Entity', "
            "{RELATED:{orientation:'UNDIRECTED', properties:'weight'}}, "
            "{readConcurrency:1, validateRelationships:false}) YIELD graphName RETURN graphName",
            name=name,
            _timeout=timeout,
        )

    def gds_algorithm_estimate(self, name: str, algorithm: str, timeout: float) -> int:
        """Оценка памяти (байты) для `pageRank` или `leiden` на проекции `name`.

        Имя алгоритма подставляется в текст запроса, поэтому разрешены только два значения;
        иначе `ValueError`."""
        if algorithm not in {"pageRank", "leiden"}:
            raise ValueError("неподдерживаемый алгоритм графа")
        return int(
            self._run(
                f"CALL gds.{algorithm}.stream.estimate($name, "
                "{relationshipWeightProperty:'weight', concurrency:1}) "
                "YIELD bytesMax RETURN bytesMax AS bytes",
                name=name,
                _timeout=timeout,
            )[0]["bytes"]
        )

    def gds_pagerank(self, name: str, timeout: float) -> list[dict]:
        """Взвешенный PageRank по проекции (20 итераций, затухание 0.85, один поток ради
        воспроизводимости): строки `{entity_id, pagerank}`. Ничего не пишет в граф."""
        return self._run(
            "CALL gds.pageRank.stream($name, {relationshipWeightProperty:'weight', "
            "concurrency:1, maxIterations:20, dampingFactor:0.85}) "
            "YIELD nodeId, score RETURN gds.util.asNode(nodeId).entity_id AS entity_id, "
            "score AS pagerank",
            name=name,
            _timeout=timeout,
        )

    def gds_leiden(self, name: str, timeout: float) -> list[dict]:
        """Сообщества Leiden по проекции (фиксированный seed и один поток — повторяемый
        результат): строки `{entity_id, community_id}`. Ничего не пишет в граф."""
        return self._run(
            "CALL gds.leiden.stream($name, {relationshipWeightProperty:'weight', "
            "concurrency:1, randomSeed:42}) YIELD nodeId, communityId "
            "RETURN gds.util.asNode(nodeId).entity_id AS entity_id, communityId AS community_id",
            name=name,
            _timeout=timeout,
        )

    def owned_projections(self) -> list[str]:
        """Имена GDS-проекций mnogobase (с префиксом `PROJECTION_PREFIX`), например
        оставшихся после прерванного `graph analyze`."""
        return [
            r["name"]
            for r in self._run(
                "CALL gds.graph.list() YIELD graphName "
                "WHERE graphName STARTS WITH $prefix RETURN graphName AS name",
                prefix=PROJECTION_PREFIX,
            )
        ]

    def drop_projection(self, name: str) -> None:
        """Удаляет проекцию mnogobase из памяти GDS; отсутствующая проекция — не ошибка.

        Исключения: `ValueError` для имени без `PROJECTION_PREFIX` (чужие проекции не
        трогаем)."""
        if not name.startswith(PROJECTION_PREFIX):
            raise ValueError("нельзя удалить проекцию, которую создал не mnogobase")
        self._run("CALL gds.graph.drop($name, false) YIELD graphName RETURN graphName", name=name)

    def clear_analysis(self) -> None:
        """Удаляет результаты аналитики (`graph clear`): сбрасывает запуск в
        `AnalysisState` и стирает свойства `analytics_*` у всех сущностей. Ревизия не
        меняется. Два отдельных запроса: после первого оценки уже не используются, так что
        прерывание между ними безопасно."""
        self._run(
            "MATCH (s:AnalysisState {key: 'graph'}) SET s.run_id = null, s.metadata = null, "
            "s.completed_at = null"
        )
        self._run(
            "MATCH (e:Entity) REMOVE e.analytics_run_id, e.analytics_pagerank, e.analytics_community"
        )

    def publish_analysis(self, revision: str, rows: list[dict], metadata: dict) -> bool:
        """Атомарно записывает результат `graph analyze`, если граф не менялся с его начала.

        `revision` — ревизия `AnalysisState`, прочитанная до проекции; `rows` — по строке
        `{entity_id, pagerank, community}` на каждую сущность; `metadata` — отчёт запуска.
        Возвращает False, если ревизия уже другая (граф изменился во время анализа, нужен
        повтор). Исключения: `ValueError`, если строки неполные, с повторами или с
        нечисловыми/отрицательными оценками, либо не покрывают ровно все сущности графа.

        Каждой сущности пишутся оценки с новым `run_id`, а затем `run_id` публикуется в
        `AnalysisState`; до коммита читатели видят прежний запуск."""
        if len({r["entity_id"] for r in rows}) != len(rows) or any(
            not math.isfinite(r["pagerank"]) or r["pagerank"] < 0 or not r["community"]
            for r in rows
        ):
            raise ValueError("для аналитики нужны полные, уникальные и конечные оценки")
        run_id = uuid.uuid4().hex

        def publish(tx):
            run = tx.run
            # Блокировка записи на узел состояния берётся до чтения ревизии: так публикация
            # сериализуется с записями исходных данных (они блокируют тот же узел первым).
            state = run(
                "MATCH (s:AnalysisState {key: 'graph'}) "
                "SET s.publications = coalesce(s.publications, 0) + 1 RETURN s.revision AS revision"
            ).single()
            if state is None or state["revision"] != revision:
                return False
            count = run("MATCH (e:Entity) RETURN count(e) AS n").single()["n"]
            if count != len(rows):
                raise ValueError("аналитика должна охватывать все сущности")
            written = run(
                "UNWIND $rows AS row MATCH (e:Entity {entity_id: row.entity_id}) "
                "SET e.analytics_run_id = $run, e.analytics_pagerank = row.pagerank, "
                "e.analytics_community = row.community RETURN count(e) AS n",
                rows=rows,
                run=run_id,
            ).single()["n"]
            if written != count:
                raise ValueError("аналитика должна охватывать все сущности")
            run(
                "MATCH (s:AnalysisState {key: 'graph'}) SET s.run_id = $run, "
                "s.metadata = $metadata, s.completed_at = datetime()",
                run=run_id,
                metadata=json.dumps(metadata, ensure_ascii=False),
            ).consume()
            return True

        with self._driver.session() as session:
            return session.execute_write(publish)

    # ---- wiki-страницы ----
    def upsert_wiki_page(
        self, page: WikiPageRecord, entity_id: str, links_to: list[str], cites: list[str]
    ) -> None:
        """Узел страницы и её рёбра одной транзакцией: сбой посередине не оставит страницу
        без CITES (иначе устаревшие цитаты пережили бы удаление документа).

        Шаги: MERGE узла со снятием флага `stale` и ребром ABOUT к сущности; удаление всех
        прежних LINKS_TO и CITES; создание новых к существующим страницам `links_to` и
        чанкам `cites` (отсутствующие цели молча пропускаются). Аналитику не сбрасывает:
        wiki-страницы не входят в проекцию GDS."""
        params = page.model_dump(exclude={"stale"})

        def write(tx):
            tx.run(
                "MERGE (p:WikiPage {page_id: $page_id}) "
                "SET p.slug = $slug, p.title = $title, p.path = $path, "
                "p.content_hash = $content_hash, p.version = $version, p.stale = false, "
                "p.updated_at = datetime() "
                "WITH p MATCH (e:Entity {entity_id: $entity_id}) MERGE (p)-[:ABOUT]->(e)",
                entity_id=entity_id,
                **params,
            ).consume()
            tx.run(
                "MATCH (p:WikiPage {page_id: $page_id})-[r:LINKS_TO|CITES]->() DELETE r",
                page_id=page.page_id,
            ).consume()
            tx.run(
                "MATCH (p:WikiPage {page_id: $page_id}) UNWIND $links AS target "
                "MATCH (q:WikiPage {page_id: target}) MERGE (p)-[:LINKS_TO]->(q)",
                page_id=page.page_id,
                links=links_to,
            ).consume()
            tx.run(
                "MATCH (p:WikiPage {page_id: $page_id}) UNWIND $cites AS cid "
                "MATCH (c:Chunk {chunk_id: cid}) MERGE (p)-[:CITES]->(c)",
                page_id=page.page_id,
                cites=cites,
            ).consume()

        with self._driver.session() as session:
            session.execute_write(write)

    def invalidate_wiki_page(self, page_id: str) -> None:
        """Страница устарела (её файл и векторы удалены до пересборки): узел, slug и входящие
        ссылки остаются, но для индекса, reindex и API страницы нет, пока wiki build её не
        пересоберёт."""
        self._run("MATCH (p:WikiPage {page_id: $id}) SET p.stale = true", id=page_id)

    def delete_wiki_page(self, page_id: str) -> None:
        """Удаляет узел страницы и его рёбра; сущность, которой посвящена страница, остаётся."""
        self._run("MATCH (p:WikiPage {page_id: $id}) DETACH DELETE p", id=page_id)

    def pages_linking_to(self, page_ids: list[str]) -> list[str]:
        """Id страниц с LINKS_TO на любую из `page_ids`, не считая самих `page_ids`."""
        rows = self._run(
            "MATCH (p:WikiPage)-[:LINKS_TO]->(q:WikiPage) "
            "WHERE q.page_id IN $ids AND NOT p.page_id IN $ids "
            "RETURN DISTINCT p.page_id AS id ORDER BY id",
            ids=page_ids,
        )
        return [r["id"] for r in rows]

    def wiki_page(self, page_id: str) -> WikiPageRecord | None:
        """Страница по id (в том числе устаревшая, с `stale=True`) или None."""
        rows = self._run(f"MATCH (p:WikiPage {{page_id: $id}}) RETURN {_PAGE_FIELDS}", id=page_id)
        return WikiPageRecord(**rows[0]["page"]) if rows else None

    def wiki_pages(self, *, include_stale: bool = False) -> list[PageIndexRow]:
        """Страницы wiki; устаревшие (без файла до пересборки) — только с `include_stale`:
        они держат slug и путь, но в индекс, reindex и API не попадают."""
        rows = self._run(
            "MATCH (p:WikiPage)-[:ABOUT]->(e:Entity) "
            "WHERE $include_stale OR NOT coalesce(p.stale, false) "
            "RETURN p.page_id AS page_id, p.slug AS slug, p.title AS title, p.path AS path, "
            "e.type AS entity_type, coalesce(e.aliases, []) AS aliases "
            "ORDER BY entity_type, title",
            include_stale=include_stale,
        )
        return [PageIndexRow(**r) for r in rows]


def _delete_document_tx(
    tx: ManagedTransaction, doc_id: str, extra_entity_ids: list[str]
) -> DeleteResult:
    """Тело транзакции `GraphStore.delete_document`.

    Шаги: сбросить аналитику; найти чанки документа и упомянутые ими сущности (плюс
    `extra_entity_ids`); убрать чанки документа из подтверждений связей; удалить чанки и
    Document; пересчитать `mention_count` затронутых сущностей и удалить те, что остались без
    упоминаний, вместе с их wiki-страницами. Повтор после коммита ничего не находит и только
    пересчитывает `extra_entity_ids`."""
    run = tx.run

    run(_INVALIDATE_ANALYSIS).consume()
    found = run(
        "MATCH (d:Document {doc_id: $doc_id}) "
        "OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:Chunk) "
        "OPTIONAL MATCH (c)-[:MENTIONS]->(e:Entity) "
        "RETURN collect(DISTINCT c.chunk_id) AS cids, collect(DISTINCT e.entity_id) AS eids",
        doc_id=doc_id,
    ).data()
    cids = found[0]["cids"] if found else []
    # сущности без MENTIONS от этого документа тоже пересчитываются (и удаляются, если их
    # больше ничто не упоминает): их оставила упавшая посреди чанка стадия graph
    eids = sorted(set(found[0]["eids"] if found else []) | set(extra_entity_ids))
    if cids:
        # убираем подтверждения из удалённых чанков; рёбра, оставшиеся без подтверждений, исчезают.
        # Связь подтверждает чанк, который упоминает оба её конца, поэтому искать её
        # достаточно среди исходящих связей сущностей документа, а не по всему графу.
        # По шагам: keep — индексы evidence, не принадлежащие документу; evidence и
        # strengths фильтруются по ним синхронно; описание очищается (оно могло прийти из
        # удалённого чанка, а откуда — не записано); вес пересчитывается как сумма
        # оставшихся сил; связь с пустым evidence удаляется.
        run(
            "MATCH (a:Entity)-[r:RELATED]->(:Entity) "
            "WHERE a.entity_id IN $eids AND any(x IN r.evidence WHERE x IN $cids) "
            "WITH r, [i IN range(0, size(r.evidence) - 1) WHERE NOT r.evidence[i] IN $cids] AS keep "
            "SET r.description = '', r.evidence = [i IN keep | r.evidence[i]], "
            "    r.strengths = [i IN keep | r.strengths[i]] "
            "SET r.weight = reduce(s = 0.0, x IN r.strengths | s + x) "
            "WITH r WHERE size(r.evidence) = 0 DELETE r",
            cids=cids,
            eids=eids,
        ).consume()
    # чанки и сам документ со всеми их связями (HAS_CHUNK, NEXT, MENTIONS, CITES)
    run(
        "MATCH (d:Document {doc_id: $doc_id}) "
        "OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:Chunk) "
        "WITH d, collect(c) AS cs "
        "FOREACH (x IN cs | DETACH DELETE x) "
        "DETACH DELETE d",
        doc_id=doc_id,
    ).consume()
    # для каждой затронутой сущности: пересчитать mention_count; если 0 — собрать её
    # wiki-страницы (id и путь для удаления файлов), удалить страницы и саму сущность
    removed = run(
        "UNWIND $eids AS eid "
        "MATCH (e:Entity {entity_id: eid}) "
        "SET e.mention_count = COUNT { (e)<-[:MENTIONS]-(:Chunk) } "
        "WITH e WHERE e.mention_count = 0 "
        "OPTIONAL MATCH (p:WikiPage)-[:ABOUT]->(e) "
        "WITH e, e.entity_id AS eid, e.name AS name, collect(p) AS pages "
        "WITH e, eid, name, pages, [x IN pages | [x.page_id, x.path]] AS page_refs "
        "FOREACH (x IN pages | DETACH DELETE x) "
        "DETACH DELETE e "
        "RETURN eid, name, page_refs",
        eids=eids,
    ).data()
    return DeleteResult(
        affected=eids,
        removed_entity_ids=[r["eid"] for r in removed],
        removed_names=[r["name"] for r in removed],
        removed_pages=[(p[0], p[1]) for r in removed for p in r["page_refs"]],
    )
