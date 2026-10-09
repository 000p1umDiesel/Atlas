from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from mnogobase.config import LANGUAGE_NAMES, WikiSettings
from mnogobase.embedding.base import Embedder, SparseEncoder
from mnogobase.ids import normalize_name, slugify
from mnogobase.llm.client import LLMClient
from mnogobase.llm.templates import render
from mnogobase.log import get_logger
from mnogobase.models import (
    EmbedInput,
    EntityRecord,
    RelationView,
    SearchHit,
    WikiPageRecord,
)
from mnogobase.progress import NULL_PROGRESS, ProgressSink
from mnogobase.registry import Registry
from mnogobase.stores.graph_store import GraphStore
from mnogobase.stores.qdrant_store import QdrantStore
from mnogobase.wiki.render import (
    SourceRef,
    append_log,
    parse_page,
    render_index,
    render_page,
    split_sections,
)
from mnogobase.wiki.validate import (
    cited_ids,
    resolve_links,
    strip_reserved_sections,
    validate_citations,
)

# нормализованное имя/алиас -> (entity_id, slug, title)
PageLookup = dict[str, tuple[str, str, str]]
_TITLE = re.compile(r"^# (.+)$", re.MULTILINE)


@dataclass
class WikiReport:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


@dataclass
class _Draft:
    entity: EntityRecord
    hits: list[SearchHit]
    rel_path: str
    slug: str
    relations: list[RelationView]
    previous: WikiPageRecord | None
    body: str  # сырой ответ LLM


@dataclass
class _Built:
    entity: EntityRecord
    record: WikiPageRecord
    links_to: list[str]
    cited: list[str]
    created: bool


def _evidence_line(hit: SearchHit) -> str:
    where = Path(hit.payload.get("path") or "unknown").name
    if hit.payload.get("page"):
        where += f", p.{hit.payload['page']}"
    return f"[{hit.key}] ({where})\n{hit.payload.get('text', '')}"


class WikiBuilder:
    def __init__(
        self,
        settings: WikiSettings,
        graph: GraphStore,
        vectors: QdrantStore,
        embedder: Embedder,
        sparse: SparseEncoder,
        llm: LLMClient,
        registry: Registry,
    ):
        self._s = settings
        self._graph = graph
        self._vectors = vectors
        self._embedder = embedder
        self._sparse = sparse
        self._llm = llm
        self._registry = registry
        self._log = get_logger(__name__)

    async def build(
        self,
        *,
        rebuild_all: bool = False,
        run_id: str = "",
        deleted: list[str] | None = None,
        documents: list[str] | None = None,
        progress: ProgressSink = NULL_PROGRESS,
    ) -> WikiReport:
        dirty = set(self._registry.dirty())
        eligible = self._graph.entities(min_mentions=self._s.min_mentions)
        eligible_ids = {e.entity_id for e in eligible}
        report = WikiReport(deleted=list(deleted or []))
        dropped = [
            page
            for page in (self._graph.wiki_page(eid) for eid in sorted(dirty - eligible_ids))
            if page is not None
        ]
        if dropped:
            # страницы, ссылающиеся на удалённую, пересобираются, чтобы устаревшая ссылка
            # исчезла; они помечаются dirty, чтобы неудачную пересборку повторил следующий build
            linking = set(self._graph.pages_linking_to([p.page_id for p in dropped]))
            linking &= eligible_ids
            self._registry.mark_dirty(linking)
            dirty |= linking
            report.deleted += self._delete_pages(dropped)
        candidates = [e for e in eligible if rebuild_all or e.entity_id in dirty]

        # сначала подтверждения: кандидат без них не получает страницу, и ссылаться на него нельзя
        evidence: dict[str, list[SearchHit]] = {}
        failed_ids: set[str] = set()
        progress.step("wiki evidence", len(candidates))
        for entity in candidates:
            try:
                evidence[entity.entity_id] = self._evidence(entity)
            except Exception as exc:  # одна плохая сущность не должна ронять весь build
                self._record_failure(report, failed_ids, entity, exc)
                progress.advance(failed=True)
            else:
                progress.advance()
        writable = [e for e in candidates if evidence.get(e.entity_id)]
        report.skipped = [e.name for e in candidates if evidence.get(e.entity_id) == []]

        slugs = self._assign_slugs(writable)

        async def draft_safely(entity: EntityRecord) -> _Draft | None:
            try:
                draft = await self._draft(
                    entity, evidence[entity.entity_id], slugs[entity.entity_id]
                )
            except Exception as exc:  # одна плохая сущность не должна ронять весь build
                self._record_failure(report, failed_ids, entity, exc)
                progress.advance(failed=True)
                return None
            progress.advance()
            return draft

        progress.step("wiki draft", len(writable))
        drafts = [d for d in await asyncio.gather(*map(draft_safely, writable)) if d is not None]
        # ссылки могут вести только на существующие страницы или записываемые в этом запуске
        drafted = {d.entity.entity_id for d in drafts}
        lookup = self._page_lookup([e for e in writable if e.entity_id in drafted], slugs)
        built: list[_Built] = []
        progress.step("wiki write", len(drafts))
        for draft in drafts:
            try:
                built.append(self._write(draft, lookup))
            except Exception as exc:  # одна плохая сущность не должна ронять весь build
                self._record_failure(report, failed_ids, draft.entity, exc)
                progress.advance(failed=True)
            else:
                progress.advance()
        # два прохода, чтобы LINKS_TO мог указывать на страницы, созданные в этом же запуске
        for b in built:
            self._graph.upsert_wiki_page(b.record, b.entity.entity_id, [], b.cited)
        for b in built:
            self._graph.upsert_wiki_page(b.record, b.entity.entity_id, b.links_to, b.cited)
            (report.created if b.created else report.updated).append(b.entity.name)
        # упавшие сущности остаются dirty, чтобы следующий build их повторил
        self._registry.clear_dirty(dirty - failed_ids)
        self._s.dir.mkdir(parents=True, exist_ok=True)
        (self._s.dir / "index.md").write_text(
            render_index(self._graph.wiki_pages()), encoding="utf-8"
        )
        if report.created or report.updated or report.deleted or documents:
            append_log(
                self._s.dir / "log.md",
                run_id,
                documents=sorted(documents or []),
                created=sorted(report.created),
                updated=sorted(report.updated),
                deleted=sorted(report.deleted),
            )
        report.failed.sort()
        self._log.info(
            "wiki_built",
            created=len(report.created),
            updated=len(report.updated),
            deleted=len(report.deleted),
            skipped=len(report.skipped),
            failed=len(report.failed),
        )
        return report

    def index_page(self, page_id: str, entity_id: str, rel_path: str, text: str) -> int:
        """Эмбеддит отрендеренную страницу в Qdrant по секциям; возвращает число секций."""
        sections = split_sections(text)
        if not sections:
            self._vectors.delete_wiki_page(page_id)
            return 0
        title_match = _TITLE.search(text)
        title = title_match.group(1).strip() if title_match else None
        texts = [s.text for s in sections]
        self._vectors.upsert_wiki_sections(
            page_id,
            entity_id,
            rel_path,
            sections,
            self._embedder.embed_documents([EmbedInput(text=t, title=title) for t in texts]),
            self._sparse.encode_documents(texts),
        )
        return len(sections)

    def _record_failure(
        self, report: WikiReport, failed_ids: set[str], entity: EntityRecord, exc: Exception
    ) -> None:
        failed_ids.add(entity.entity_id)
        report.failed.append(entity.name)
        self._log.warning(
            "wiki_page_failed",
            entity_id=entity.entity_id,
            error_type=type(exc).__name__,
            error=str(exc),
        )

    def _delete_pages(self, pages: list[WikiPageRecord]) -> list[str]:
        """Удаляет файлы страниц, секции в Qdrant и узлы графа; возвращает заголовки страниц."""
        for page in pages:
            (self._s.dir / page.path).unlink(missing_ok=True)
            self._vectors.delete_wiki_page(page.page_id)
            self._graph.delete_wiki_page(page.page_id)
        return [page.title for page in pages]

    def _evidence(self, entity: EntityRecord) -> list[SearchHit]:
        query = self._embedder.embed_query(f"{entity.name}: {entity.description}")
        return self._vectors.search_chunks_for_entity(query, entity.entity_id, self._s.evidence_k)

    def _carried_evidence(
        self, entity: EntityRecord, existing_body: str, hits: list[SearchHit]
    ) -> list[SearchHit]:
        """Чанки, которые цитирует существующая страница, но которые выпали из свежего top-k:
        остаются подтверждениями (чтобы их цитаты пережили обновление), пока существуют и всё
        ещё упоминают сущность; не больше `evidence_k` штук."""
        fresh = {h.key for h in hits}
        previous = [c for c in cited_ids(existing_body) if c not in fresh]
        if not previous:
            return []
        chunks = self._graph.chunks_mentioning(entity.entity_id, previous)
        return [
            SearchHit(
                key=c.chunk_id,
                score=0.0,
                payload={"chunk_id": c.chunk_id, "text": c.text, "path": c.path, "page": c.page},
            )
            for c in chunks[: self._s.evidence_k]
        ]

    def _assign_slugs(self, candidates: list[EntityRecord]) -> dict[str, str]:
        taken = {row.slug: row.page_id for row in self._graph.wiki_pages()}
        slugs: dict[str, str] = {}
        for entity in candidates:
            page = self._graph.wiki_page(entity.entity_id)
            if page is not None:
                slugs[entity.entity_id] = page.slug
        for entity in candidates:
            if entity.entity_id in slugs:
                continue
            base = slugify(entity.name)
            options = (base, f"{base}-{slugify(entity.type)}", f"{base}-{entity.entity_id[:6]}")
            slug = next(o for o in options if taken.get(o, entity.entity_id) == entity.entity_id)
            taken[slug] = entity.entity_id
            slugs[entity.entity_id] = slug
        return slugs

    def _page_lookup(self, candidates: list[EntityRecord], slugs: dict[str, str]) -> PageLookup:
        lookup: PageLookup = {}
        for row in self._graph.wiki_pages():
            for name in (row.title, *row.aliases):
                lookup.setdefault(normalize_name(name), (row.page_id, row.slug, row.title))
        for entity in candidates:
            for name in (entity.name, *entity.aliases):
                lookup.setdefault(
                    normalize_name(name), (entity.entity_id, slugs[entity.entity_id], entity.name)
                )
        return lookup

    async def _draft(self, entity: EntityRecord, hits: list[SearchHit], slug: str) -> _Draft:
        """Собирает входные данные страницы и запрашивает у LLM её тело."""
        context = self._graph.entity_context(entity.entity_id, max_relations=30)
        rel_path = f"entities/{slug}.md"
        file = self._s.dir / rel_path
        previous = self._graph.wiki_page(entity.entity_id)
        existing_body = parse_page(file.read_text(encoding="utf-8"))[1] if file.exists() else ""
        hits = hits + self._carried_evidence(entity, existing_body, hits)
        prompt = render(
            "wiki_page",
            language=LANGUAGE_NAMES.get(self._s.language, self._s.language),
            name=entity.name,
            type=entity.type,
            description=entity.description or "-",
            aliases=", ".join(entity.aliases) or "-",
            relations="\n".join(
                f"- {r.src_name} {r.predicate} {r.dst_name}: {r.description}"
                for r in context.relations
            )
            or "-",
            evidence="\n\n".join(_evidence_line(h) for h in hits),
            example_id=hits[0].key,
            existing=existing_body,
        )
        body = await self._llm.complete([{"role": "user", "content": prompt}], task="wiki")
        return _Draft(entity, hits, rel_path, slug, context.relations, previous, body)

    def _write(self, draft: _Draft, lookup: PageLookup) -> _Built:
        """Валидирует и рендерит тело от LLM, записывает файл страницы и индексирует её."""
        entity, hits = draft.entity, draft.hits
        body = strip_reserved_sections(draft.body)
        body, cited = validate_citations(body, {h.key for h in hits})
        body, linked_ids = resolve_links(body, lookup)

        page_slugs = {eid: s for eid, s, _ in lookup.values()}
        neighbours = {r.src_id for r in draft.relations} | {r.dst_id for r in draft.relations}
        linked = {
            eid: page_slugs[eid]
            for eid in neighbours
            if eid in page_slugs and eid != entity.entity_id
        }
        payloads = {h.key: h.payload for h in hits}
        sources = [SourceRef(c, payloads[c].get("path"), payloads[c].get("page")) for c in cited]
        version = draft.previous.version + 1 if draft.previous else 1
        text = render_page(entity, body, draft.relations, linked, sources, version, date.today())
        file = self._s.dir / draft.rel_path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding="utf-8")
        self.index_page(entity.entity_id, entity.entity_id, draft.rel_path, text)

        record = WikiPageRecord(
            page_id=entity.entity_id,
            slug=draft.slug,
            title=entity.name,
            path=draft.rel_path,
            content_hash=hashlib.sha1(text.encode("utf-8")).hexdigest(),
            version=version,
        )
        links_to = sorted((set(linked_ids) | set(linked)) - {entity.entity_id})
        return _Built(entity, record, links_to, cited, created=draft.previous is None)
