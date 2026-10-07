from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

from mnogobase.chunking.hybrid import Chunker
from mnogobase.config import Settings
from mnogobase.embedding.base import Embedder, SparseEncoder, embedder_signature
from mnogobase.extraction.extractor import TYPES_CHANGED, Extractor, stale_entity_types
from mnogobase.extraction.resolver import EntityResolver
from mnogobase.ids import file_doc_id, normalize_name
from mnogobase.log import bind_context, get_logger, log_stage, unbind_context
from mnogobase.models import ChunkRecord, DocumentRecord, EmbedInput
from mnogobase.parsing.docling_parser import DoclingParser
from mnogobase.registry import Registry
from mnogobase.stores.graph_store import GraphStore
from mnogobase.stores.qdrant_store import QdrantStore
from mnogobase.wiki.builder import WikiBuilder, WikiReport


class EmbedderMismatchError(RuntimeError):
    pass


class ExtractionFailedError(RuntimeError):
    pass


@dataclass
class IngestReport:
    processed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    wiki: WikiReport | None = None  # per-page wiki failures are in `wiki.failed`
    # set when the graph holds entities typed with another entity type set than the config
    types_warning: str | None = None


class _PreviouslyFailed(Exception):
    pass


_UNREPORTED_REMOVALS = "unreported_removed_entities"  # registry meta key


class PendingRemovalError(RuntimeError):
    """A journaled document removal from an earlier run still cannot be finished."""


class _RemovalPlan(BaseModel):
    """What removing a document cleans up; journaled in the registry until it is done.

    Computed read-only right before the graph transaction, so it matches what that
    transaction removes."""

    removed_entities: dict[str, str]  # entity_id -> name
    removed_pages: list[tuple[str, str]]  # (page_id, wiki-relative path)
    dirty: list[str]  # surviving entities whose pages must be regenerated
    relinked: int  # how many of those only linked to a removed page


def _holds(path: str, doc_id: str) -> bool:
    """Whether the file at `path` exists and currently has this content (registry rows and
    recorded document paths can be stale: the file was renamed, deleted or edited)."""
    file = Path(path)
    return file.is_file() and file_doc_id(file) == doc_id


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        registry: Registry,
        parser: DoclingParser,
        chunker: Chunker,
        embedder: Embedder,
        sparse: SparseEncoder,
        vectors: QdrantStore,
        graph: GraphStore,
        extractor: Extractor,
        resolver: EntityResolver,
        wiki: WikiBuilder,
    ):
        self._s = settings
        self._registry = registry
        self._parser = parser
        self._chunker = chunker
        self._embedder = embedder
        self._sparse = sparse
        self._vectors = vectors
        self._graph = graph
        self._extractor = extractor
        self._resolver = resolver
        self._wiki = wiki
        self._log = get_logger(__name__)

    # ---- setup ----
    def prepare(self, check_embedder: bool = True, resume: bool = True) -> None:
        signature = embedder_signature(self._embedder)
        stored = self._registry.get_meta("embedder")
        if check_embedder and stored is not None and stored != signature:
            raise EmbedderMismatchError(
                f"index was built with {stored}, config now uses {signature}; "
                "run `mnogobase reindex`"
            )
        self._vectors.ensure_collections()
        self._graph.ensure_schema()
        if stored is None:
            self._registry.set_meta("embedder", signature)
        if resume:
            resumed = self._registry.reset_running()
            if resumed:
                self._log.warning("resuming_interrupted_stages", count=resumed)

    def discover(self, paths: Sequence[Path]) -> list[Path]:
        exts = {f".{e.lower().lstrip('.')}" for e in self._s.parsing.extensions}
        found: list[Path] = []
        for path in paths:
            path = Path(path)
            if path.is_dir():
                for file in sorted(path.rglob("*")):
                    hidden = any(part.startswith(".") for part in file.relative_to(path).parts)
                    if file.is_file() and file.suffix.lower() in exts and not hidden:
                        found.append(file)
            elif path.is_file() and path.suffix.lower() in exts:
                found.append(path)
        return list(dict.fromkeys(f.resolve() for f in found))

    # ---- chunk cache ----
    def chunks_path(self, doc_id: str) -> Path:
        return self._s.data_dir / "cache" / f"{doc_id}.chunks.json"

    def _save_chunks(self, doc: DocumentRecord, chunks: list[ChunkRecord]) -> None:
        path = self.chunks_path(doc.doc_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"document": doc.model_dump(), "chunks": [c.model_dump() for c in chunks]}
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def load_chunks(self, doc_id: str) -> tuple[DocumentRecord, list[ChunkRecord]]:
        payload = json.loads(self.chunks_path(doc_id).read_text(encoding="utf-8"))
        return (
            DocumentRecord(**payload["document"]),
            [ChunkRecord(**c) for c in payload["chunks"]],
        )

    # ---- ingest ----
    async def ingest(
        self,
        paths: Sequence[Path],
        *,
        build_wiki: bool = True,
        retry_failed: bool = False,
        run_id: str = "",
    ) -> IngestReport:
        self.prepare()
        report = IngestReport()
        removed_names = self.finish_pending_removals()
        for path in self.discover(paths):
            key = str(path)
            try:
                status = await self._ingest_file(path, retry_failed, removed_names)
            except _PreviouslyFailed as exc:
                report.failed[key] = str(exc)
                continue
            except Exception as exc:
                report.failed[key] = f"{type(exc).__name__}: {exc}"
                self._log.error(
                    "ingest_file_failed",
                    path=key,
                    error_type=type(exc).__name__,
                    error=str(exc),
                    exc_info=True,
                )
                continue
            (report.processed if status == "processed" else report.skipped).append(key)
        if stale_entity_types(self._registry, self._s.extract.entity_types):
            report.types_warning = TYPES_CHANGED
            self._log.warning("entity_types_changed", detail=TYPES_CHANGED)
        if build_wiki:
            report.wiki = await self._wiki.build(
                run_id=run_id, deleted=removed_names, documents=report.processed
            )
        self._log.info(
            "ingest_done",
            processed=len(report.processed),
            skipped=len(report.skipped),
            failed=len(report.failed),
            wiki_failed=len(report.wiki.failed) if report.wiki else 0,
        )
        return report

    async def _ingest_file(self, path: Path, retry_failed: bool, removed_names: list[str]) -> str:
        key = str(path)
        doc_id = file_doc_id(path)
        stat = path.stat()
        row = self._registry.get_file(key)
        if row is not None and row.doc_id != doc_id:
            removed_names.extend(self._remove_doc(row.doc_id, keep_path=key))
        journaled = self._registry.get_removal(doc_id)
        if journaled is not None:
            # this content's removal failed earlier in this run: settle it first with this
            # file as a live copy, so the content is neither deleted nor skipped as done
            # while its data is gone
            plan = _RemovalPlan.model_validate_json(journaled)
            removed_names.extend(self._resume_removal(doc_id, plan, live=[key]))
        pending = self._registry.pending_stages(doc_id)
        if not pending:
            self._registry.upsert_file(key, doc_id, stat.st_size, stat.st_mtime, "done")
            self._follow_moved_document(doc_id, key)
            return "skipped"
        failed_stage = next(
            (s for s in pending if self._registry.stage_status(doc_id, s) == "failed"), None
        )
        if failed_stage and not retry_failed:
            self._registry.upsert_file(key, doc_id, stat.st_size, stat.st_mtime, "failed")
            raise _PreviouslyFailed(
                f"stage {failed_stage} failed earlier; rerun with --retry-failed"
            )
        self._registry.upsert_file(key, doc_id, stat.st_size, stat.st_mtime, "processing")
        bind_context(doc_id=doc_id)
        try:
            for stage in pending:
                self._registry.set_stage(doc_id, stage, "running")
                try:
                    with log_stage(self._log, stage, path=key):
                        await self._run_stage(stage, path, doc_id)
                except Exception as exc:
                    self._registry.set_stage(
                        doc_id, stage, "failed", error=f"{type(exc).__name__}: {exc}"
                    )
                    self._registry.set_file_status(key, "failed")
                    raise
                self._registry.set_stage(doc_id, stage, "done")
        finally:
            unbind_context("doc_id")
        self._registry.set_file_status(key, "done")
        return "processed"

    def _remove_doc(self, doc_id: str, keep_path: str) -> list[str]:
        """Delete an old document version unless another path still has the same content."""
        journaled = self._registry.get_removal(doc_id)
        if journaled is not None:  # an interrupted earlier attempt
            return self._resume_removal(doc_id, _RemovalPlan.model_validate_json(journaled))
        # only a file that still has this content keeps it (not a renamed or edited one)
        survivors = [p for p in self._live_copies(doc_id) if p != keep_path]
        if survivors:
            self._repoint_doc(doc_id, survivors)
            return []
        return self._start_removal(doc_id)

    def finish_pending_removals(self) -> list[str]:
        """Finish document removals an earlier run started but did not complete (crash or a
        store error); returns the names of the removed entities."""
        # names of removals finished by an earlier call that then failed on another one
        names: list[str] = json.loads(self._registry.get_meta(_UNREPORTED_REMOVALS) or "[]")
        failed: dict[str, Exception] = {}
        for doc_id, plan_json in self._registry.pending_removals():
            self._log.warning("resuming_document_removal", old_doc_id=doc_id)
            try:
                names += self._resume_removal(doc_id, _RemovalPlan.model_validate_json(plan_json))
            except Exception as exc:  # the others are independent: finish them anyway
                failed[doc_id] = exc
        if failed:
            # kept for the wiki report / log.md of the run that finally succeeds
            self._registry.set_meta(_UNREPORTED_REMOVALS, json.dumps(names))
            causes = "; ".join(f"{d}: {type(e).__name__}: {e}" for d, e in failed.items())
            raise PendingRemovalError(
                f"cannot finish removing old document version(s) {', '.join(failed)} "
                f"({causes}). Fix the cause (see `mnogobase doctor`) and rerun: the removal "
                "is retried first, nothing else runs until it is done. `mnogobase reset` "
                "starts over instead."
            ) from next(iter(failed.values()))
        self._registry.set_meta(_UNREPORTED_REMOVALS, "[]")
        return names

    def _start_removal(self, doc_id: str) -> list[str]:
        plan = self._removal_plan(doc_id)
        self._registry.put_removal(doc_id, plan.model_dump_json())
        return self._apply_removal(doc_id, plan)

    def _resume_removal(
        self, doc_id: str, plan: _RemovalPlan, live: Sequence[str] = ()
    ) -> list[str]:
        """Finish an interrupted removal, re-validated against what is true now.

        A file with this content may exist again (another path, a new file, a revert): such
        live copies must not lose the document. `live` names paths known to hold it."""
        survivors = self._live_copies(doc_id, live)
        if self._graph.has_document(doc_id):
            # the graph transaction never committed, so nothing was deleted yet
            if survivors:
                self._repoint_doc(doc_id, survivors)
                self._registry.drop_removal(doc_id)
                self._log.info("document_removal_dropped", old_doc_id=doc_id, kept_by=survivors)
                return []
            # later work (other files of that run) may have changed what removal implies
            return self._start_removal(doc_id)
        return self._apply_removal(doc_id, plan, survivors)

    def _live_copies(self, doc_id: str, live: Sequence[str] = ()) -> list[str]:
        """Paths whose file currently holds this content (registry rows can be stale)."""
        candidates = dict.fromkeys([*self._registry.paths_for_doc(doc_id), *live])
        return [p for p in candidates if _holds(p, doc_id)]

    def _removal_plan(self, doc_id: str) -> _RemovalPlan:
        """Read-only: everything a removal must clean up, computed while the graph still has it."""
        linkers = self._pages_linking_into(doc_id)
        result = self._graph.document_deletion_plan(doc_id)
        removed = set(result.removed_entity_ids)
        # pages linking to a removed page are rewritten so the stale link disappears
        stale_linkers = {p for target in removed for p in linkers.get(target, [])}
        return _RemovalPlan(
            removed_entities=dict(
                zip(result.removed_entity_ids, result.removed_names, strict=True)
            ),
            removed_pages=result.removed_pages,
            dirty=sorted((set(result.affected) | stale_linkers) - removed),
            relinked=len(stale_linkers - removed),
        )

    def _apply_removal(
        self, doc_id: str, plan: _RemovalPlan, survivors: Sequence[str] = ()
    ) -> list[str]:
        """Apply a journaled removal: graph transaction, then Qdrant, wiki files and registry.

        Idempotent, and safe to replay after other work: once the graph transaction has
        committed, the graph is the truth. An entity or page the plan lists but the graph
        has again (re-mentioned or recreated under the same id by a later file) is live
        data: its point, sections and file stay and it is regenerated (marked dirty).
        `survivors` are live copies of the content found after the graph transaction had
        committed: their data is gone, so their stages are reset (caches kept) and they are
        ingested again instead of staying "done".
        Returns the names of the entities that are really gone."""
        self._graph.delete_document(doc_id)  # one transaction; a no-op once committed
        gone = {e: n for e, n in plan.removed_entities.items() if self._graph.get_entity(e) is None}
        revived = set(plan.removed_entities) - set(gone)
        self._registry.mark_dirty(set(plan.dirty) | revived)
        self._registry.clear_dirty(gone)
        self._vectors.delete_doc(doc_id)
        self._vectors.delete_entities(sorted(gone))
        # a freed slug may already belong to another entity's page written later
        live_paths = {row.path for row in self._graph.wiki_pages()} if plan.removed_pages else set()
        for page_id, rel_path in plan.removed_pages:
            if self._graph.wiki_page(page_id) is None:
                self._vectors.delete_wiki_page(page_id)
            if rel_path not in live_paths:
                (self._s.wiki.dir / rel_path).unlink(missing_ok=True)
        if survivors:
            self._registry.reset_stages(doc_id)
            for path in survivors:
                self._registry.set_file_status(path, "pending")
            self._log.warning("document_needs_reingest", doc_id=doc_id, paths=list(survivors))
        else:
            self._registry.clear_doc(doc_id)
            self._parser.drop_cache(doc_id)
            self.chunks_path(doc_id).unlink(missing_ok=True)
        self._registry.drop_removal(doc_id)
        self._log.info(
            "document_removed",
            old_doc_id=doc_id,
            removed_entities=len(gone),
            revived_entities=len(revived),
            relinked_pages=plan.relinked,
        )
        return sorted(gone.values())

    def _pages_linking_into(self, doc_id: str) -> dict[str, list[str]]:
        """Before a cascade delete: for each wiki page of an entity this document mentions,
        the pages that link to it (page_id == entity_id). The edges vanish with the delete."""
        mentioned = {e for ids in self._graph.chunk_entity_ids(doc_id).values() for e in ids}
        paged = [row.page_id for row in self._graph.wiki_pages() if row.page_id in mentioned]
        return {page_id: self._graph.pages_linking_to([page_id]) for page_id in paged}

    def _follow_moved_document(self, doc_id: str, path: str) -> None:
        """`path` holds an already ingested document: if the path its citations name no
        longer has it (the file was renamed, deleted or edited), point them at `path`."""
        if all(p == path for p in self._registry.paths_for_doc(doc_id)):
            return  # no other file ever had this content: the citations already name `path`
        if not self.chunks_path(doc_id).exists():
            return  # never chunked: nothing stored carries a path yet
        doc, _chunks = self.load_chunks(doc_id)
        if doc.path != path and not _holds(doc.path, doc_id):
            self._repoint_doc(doc_id, [path])

    def _repoint_doc(self, doc_id: str, survivors: list[str]) -> None:
        """Shared content stays; make sure citations name a path that still has it."""
        if not self.chunks_path(doc_id).exists():
            return  # never chunked: nothing stored carries a path yet
        doc, chunks = self.load_chunks(doc_id)
        if doc.path in survivors:
            return
        path = survivors[0]
        self._log.info("document_repointed", old_path=doc.path, path=path)
        doc = doc.model_copy(update={"path": path})
        self._save_chunks(doc, [c.model_copy(update={"path": path}) for c in chunks])
        self._graph.upsert_document(doc)
        self._vectors.set_doc_path(doc_id, path)

    async def _run_stage(self, stage: str, path: Path, doc_id: str) -> None:
        if stage == "parse":
            self._parser.parse(path, doc_id)
        elif stage == "chunk":
            parsed = self._parser.load(doc_id, path)
            chunks = self._chunker.chunk(parsed, doc_id, str(path))
            doc = DocumentRecord(
                doc_id=doc_id,
                path=str(path),
                title=parsed.title,
                mime=parsed.mime,
                n_pages=parsed.n_pages,
            )
            self._save_chunks(doc, chunks)
        elif stage == "embed":
            self._embed(doc_id)
        elif stage == "extract":
            await self._extract(doc_id)
        elif stage == "graph":
            await self._build_graph(doc_id)
        else:
            raise ValueError(f"unknown stage {stage}")

    def index_chunks(self, doc: DocumentRecord, chunks: list[ChunkRecord]) -> None:
        """Embed chunks (dense + sparse) and upsert them into Qdrant."""
        if not chunks:
            return
        texts = [c.context_text for c in chunks]
        dense = self._embedder.embed_documents([EmbedInput(text=t, title=doc.title) for t in texts])
        self._vectors.upsert_chunks(chunks, dense, self._sparse.encode_documents(texts))

    def _embed(self, doc_id: str) -> None:
        doc, chunks = self.load_chunks(doc_id)
        self._graph.upsert_document(doc)
        if not chunks:
            return
        self.index_chunks(doc, chunks)
        self._graph.upsert_chunks(chunks)

    async def _extract(self, doc_id: str) -> None:
        doc, chunks = self.load_chunks(doc_id)
        if not chunks:
            return
        results = await self._extractor.extract_many(chunks, doc.title)
        failed = len(chunks) - len(results)
        if failed / len(chunks) > self._s.extract.max_failed_ratio:
            raise ExtractionFailedError(f"{failed}/{len(chunks)} chunks failed extraction")
        if failed:
            self._log.warning("extract_partial", failed=failed, total=len(chunks))

    async def _build_graph(self, doc_id: str) -> None:
        _doc, chunks = self.load_chunks(doc_id)
        # the extract stage may have run with another model / prompt version (changed before
        # a retry of this stage): its results are still this document's knowledge
        results = {c.chunk_id: self._extractor.cached(c, any_version=True) for c in chunks}
        missing = sum(1 for r in results.values() if r is None)
        if chunks and missing / len(chunks) > self._s.extract.max_failed_ratio:
            # never mark the graph done without the knowledge; a retry re-runs extract first
            self._registry.set_stage(doc_id, "extract", "pending")
            raise ExtractionFailedError(
                f"{missing}/{len(chunks)} chunks have no cached extraction; "
                "rerun with --retry-failed to extract them again"
            )
        touched: set[str] = set()
        for chunk in chunks:
            result = results[chunk.chunk_id]
            if result is None:
                continue
            ids_by_name: dict[str, str] = {}
            for extracted in result.entities:
                record = await self._resolver.resolve(extracted)
                ids_by_name[normalize_name(extracted.name)] = record.entity_id
                for alias in extracted.aliases:
                    ids_by_name.setdefault(normalize_name(alias), record.entity_id)
            entity_ids = sorted(set(ids_by_name.values()))
            self._graph.add_mentions(chunk.chunk_id, entity_ids)
            self._vectors.set_chunk_entities(chunk.chunk_id, entity_ids)
            for rel in result.relations:
                src = ids_by_name.get(normalize_name(rel.source))
                dst = ids_by_name.get(normalize_name(rel.target))
                if src and dst and src != dst:
                    self._graph.merge_relation(
                        src, dst, rel.predicate, rel.description, rel.strength, chunk.chunk_id
                    )
            touched.update(entity_ids)
        self._registry.mark_dirty(touched)
