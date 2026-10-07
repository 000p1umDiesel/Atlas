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
from mnogobase.models import EmbedInput, EntityRecord, SearchHit, WikiPageRecord
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
from mnogobase.wiki.validate import resolve_links, strip_reserved_sections, validate_citations

PageLookup = dict[str, tuple[str, str, str]]  # normalized name/alias -> (entity_id, slug, title)
_TITLE = re.compile(r"^# (.+)$", re.MULTILINE)


@dataclass
class WikiReport:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


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
    ) -> WikiReport:
        dirty = set(self._registry.dirty())
        eligible = self._graph.entities(min_mentions=self._s.min_mentions)
        candidates = [e for e in eligible if rebuild_all or e.entity_id in dirty]
        report = WikiReport(deleted=list(deleted or []))
        report.deleted += self._delete_dropped_pages(dirty - {e.entity_id for e in eligible})

        # evidence first: a candidate without evidence gets no page, so nothing may link to it
        evidence: dict[str, list[SearchHit]] = {}
        failed_ids: set[str] = set()
        for entity in candidates:
            try:
                evidence[entity.entity_id] = self._evidence(entity)
            except Exception as exc:  # one bad entity must not fail the whole build
                self._record_failure(report, failed_ids, entity, exc)
        writable = [e for e in candidates if evidence.get(e.entity_id)]
        report.skipped = [e.name for e in candidates if evidence.get(e.entity_id) == []]

        slugs = self._assign_slugs(writable)
        lookup = self._page_lookup(writable, slugs)

        async def build_safely(entity: EntityRecord) -> _Built | None:
            try:
                return await self._build_one(
                    entity, evidence[entity.entity_id], slugs[entity.entity_id], lookup
                )
            except Exception as exc:  # one bad entity must not fail the whole build
                self._record_failure(report, failed_ids, entity, exc)
                return None

        results = await asyncio.gather(*(build_safely(e) for e in writable))
        built = [b for b in results if b is not None]
        # two passes so LINKS_TO can target pages created in this same run
        for b in built:
            self._graph.upsert_wiki_page(b.record, b.entity.entity_id, [], b.cited)
        for b in built:
            self._graph.upsert_wiki_page(b.record, b.entity.entity_id, b.links_to, b.cited)
            (report.created if b.created else report.updated).append(b.entity.name)
        # failed entities stay dirty so the next build retries them
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
        """Embed a rendered page section by section into Qdrant; returns the section count."""
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

    def _delete_dropped_pages(self, entity_ids: set[str]) -> list[str]:
        """Remove pages of entities that fell below min_mentions or no longer exist."""
        removed: list[str] = []
        for entity_id in sorted(entity_ids):
            page = self._graph.wiki_page(entity_id)
            if page is None:
                continue
            (self._s.dir / page.path).unlink(missing_ok=True)
            self._vectors.delete_wiki_page(page.page_id)
            self._graph.delete_wiki_page(page.page_id)
            removed.append(page.title)
        return removed

    def _evidence(self, entity: EntityRecord) -> list[SearchHit]:
        query = self._embedder.embed_query(f"{entity.name}: {entity.description}")
        return self._vectors.search_chunks_for_entity(query, entity.entity_id, self._s.evidence_k)

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

    async def _build_one(
        self, entity: EntityRecord, hits: list[SearchHit], slug: str, lookup: PageLookup
    ) -> _Built:
        context = self._graph.entity_context(entity.entity_id, max_relations=30)
        rel_path = f"entities/{slug}.md"
        file = self._s.dir / rel_path
        previous = self._graph.wiki_page(entity.entity_id)
        existing_body = parse_page(file.read_text(encoding="utf-8"))[1] if file.exists() else ""
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
        body = strip_reserved_sections(body)
        body, cited = validate_citations(body, {h.key for h in hits})
        body, linked_ids = resolve_links(body, lookup)

        page_slugs = {eid: s for eid, s, _ in lookup.values()}
        neighbours = {r.src_id for r in context.relations} | {r.dst_id for r in context.relations}
        linked = {
            eid: page_slugs[eid]
            for eid in neighbours
            if eid in page_slugs and eid != entity.entity_id
        }
        payloads = {h.key: h.payload for h in hits}
        sources = [SourceRef(c, payloads[c].get("path"), payloads[c].get("page")) for c in cited]
        version = previous.version + 1 if previous else 1
        text = render_page(entity, body, context.relations, linked, sources, version, date.today())
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding="utf-8")
        self.index_page(entity.entity_id, entity.entity_id, rel_path, text)

        record = WikiPageRecord(
            page_id=entity.entity_id,
            slug=slug,
            title=entity.name,
            path=rel_path,
            content_hash=hashlib.sha1(text.encode("utf-8")).hexdigest(),
            version=version,
        )
        links_to = sorted((set(linked_ids) | set(linked)) - {entity.entity_id})
        return _Built(entity, record, links_to, cited, created=previous is None)
