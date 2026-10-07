from __future__ import annotations

import contextlib
import shutil

from mnogobase.app import App
from mnogobase.embedding.base import embedder_signature
from mnogobase.extraction.resolver import entity_embed_input
from mnogobase.log import get_logger

ENTITY_BATCH = 64
WIKI_FILES = ("index.md", "log.md")
# Stored while a reindex runs: an interrupted reindex leaves a partial index behind, and
# `Pipeline.prepare` then refuses to use it until `mnogobase reindex` completes.
REINDEX_IN_PROGRESS = "reindex-in-progress"


def reindex(app: App) -> dict[str, int]:
    """Recompute all vectors with the current embedder; graph and wiki files stay untouched."""
    log = get_logger(__name__)
    app.registry.set_meta("embedder", REINDEX_IN_PROGRESS)
    app.vectors.drop_collections()
    app.vectors.ensure_collections()
    app.graph.ensure_schema()
    stats = {"chunks": 0, "entities": 0, "wiki_sections": 0}

    # every document whose chunks were embedded once, whatever happened in later stages
    doc_ids = sorted({f.doc_id for f in app.registry.files()})
    for doc_id in doc_ids:
        if app.registry.stage_status(doc_id, "embed") != "done":
            continue
        if app.pipeline.chunks_path(doc_id).exists():
            doc, chunks = app.pipeline.load_chunks(doc_id)
        else:
            stored = app.graph.doc_chunks(doc_id)
            if stored is None:
                log.warning("reindex_document_missing", doc_id=doc_id)
                continue
            doc, chunks = stored
        app.pipeline.index_chunks(doc, chunks)
        for chunk_id, entity_ids in app.graph.chunk_entity_ids(doc_id).items():
            if entity_ids:
                app.vectors.set_chunk_entities(chunk_id, entity_ids)
        stats["chunks"] += len(chunks)

    entities = app.graph.entities()
    for start in range(0, len(entities), ENTITY_BATCH):
        batch = entities[start : start + ENTITY_BATCH]
        dense = app.embedder.embed_documents([entity_embed_input(e) for e in batch])
        app.vectors.upsert_entities(batch, dense)
    stats["entities"] = len(entities)

    for row in app.graph.wiki_pages():
        file = app.settings.wiki.dir / row.path
        if not file.exists():
            log.warning("reindex_wiki_page_missing", page_id=row.page_id, path=row.path)
            continue
        # page_id == entity_id: one page per entity
        text = file.read_text(encoding="utf-8")
        stats["wiki_sections"] += app.wiki.index_page(row.page_id, row.page_id, row.path, text)

    app.registry.set_meta("embedder", embedder_signature(app.embedder))
    log.info("reindex_done", **stats)
    return stats


def reset(app: App) -> None:
    """Delete every vector, graph node, registry row, cache file and wiki page.

    Only the wiki files mnogobase writes are removed (`entities/`, `index.md`, `log.md`); the
    wiki directory itself goes only if nothing else is left in it."""
    app.vectors.drop_collections()
    app.graph.wipe()
    app.registry.wipe()
    shutil.rmtree(app.settings.data_dir / "cache", ignore_errors=True)
    wiki_dir = app.settings.wiki.dir
    shutil.rmtree(wiki_dir / "entities", ignore_errors=True)
    for name in WIKI_FILES:
        (wiki_dir / name).unlink(missing_ok=True)
    with contextlib.suppress(OSError):  # missing, or holds files mnogobase does not own
        wiki_dir.rmdir()
    get_logger(__name__).info("reset_done")
