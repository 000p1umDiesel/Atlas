from __future__ import annotations

import contextlib
import shutil

from mnogobase.app import App
from mnogobase.embedding.base import embedder_signature
from mnogobase.extraction.resolver import entity_embed_input
from mnogobase.log import get_logger
from mnogobase.progress import NULL_PROGRESS, ProgressSink

ENTITY_BATCH = 64
WIKI_FILES = ("index.md", "log.md")
# Хранится, пока идёт reindex: прерванный reindex оставляет неполный индекс, и
# `Pipeline.prepare` отказывается с ним работать, пока `mnogobase reindex` не завершится.
REINDEX_IN_PROGRESS = "reindex-in-progress"


class ReindexError(RuntimeError):
    """`reindex` отказался запускаться; существующий индекс не тронут."""


def _check_embedder(app: App) -> None:
    """Проверяет, что embedder выдаёт `dim` значений, до удаления старого индекса."""
    expected = app.embedder.dim
    try:
        got = len(app.embedder.embed_query("dimension check"))
    except ValueError as exc:  # напр. truncate_normalize: модель отдаёт меньше измерений
        raise ReindexError(f"embedder check failed: {exc}; nothing was changed") from exc
    if got != expected:
        raise ReindexError(
            f"embedder check failed: it returns {got} dims, config expects {expected}; "
            "nothing was changed"
        )


def reindex(app: App, progress: ProgressSink = NULL_PROGRESS) -> dict[str, int]:
    """Пересчитывает все векторы текущим embedder; граф и файлы wiki не трогаются.

    Коллекции удаляются и создаются заново с размерностью embedder, так что это же переводит
    индекс на другую размерность."""
    log = get_logger(__name__)
    _check_embedder(app)
    app.registry.set_meta("embedder", REINDEX_IN_PROGRESS)
    app.vectors.drop_collections()
    app.vectors.ensure_collections()
    app.graph.ensure_schema()
    stats = {"chunks": 0, "entities": 0, "wiki_sections": 0}

    # каждый документ, чанки которого хоть раз были заэмбеддены, что бы ни было на поздних стадиях
    doc_ids = sorted({f.doc_id for f in app.registry.files()})
    doc_ids = [d for d in doc_ids if app.registry.stage_status(d, "embed") == "done"]
    progress.step("reindex chunks", len(doc_ids))
    for doc_id in doc_ids:
        if app.pipeline.chunks_path(doc_id).exists():
            doc, chunks = app.pipeline.load_chunks(doc_id)
        else:
            stored = app.graph.doc_chunks(doc_id)
            if stored is None:
                log.warning("reindex_document_missing", doc_id=doc_id)
                progress.advance(failed=True)
                continue
            doc, chunks = stored
        app.pipeline.index_chunks(doc, chunks)
        for chunk_id, entity_ids in app.graph.chunk_entity_ids(doc_id).items():
            if entity_ids:
                app.vectors.set_chunk_entities(chunk_id, entity_ids)
        stats["chunks"] += len(chunks)
        progress.advance()

    entities = app.graph.entities()
    progress.step("reindex entities", len(entities))
    for start in range(0, len(entities), ENTITY_BATCH):
        batch = entities[start : start + ENTITY_BATCH]
        dense = app.embedder.embed_documents([entity_embed_input(e) for e in batch])
        app.vectors.upsert_entities(batch, dense)
        progress.advance(len(batch))
    stats["entities"] = len(entities)

    pages = app.graph.wiki_pages()
    progress.step("reindex wiki", len(pages))
    for row in pages:
        file = app.settings.wiki.dir / row.path
        if not file.exists():
            log.warning("reindex_wiki_page_missing", page_id=row.page_id, path=row.path)
            progress.advance(failed=True)
            continue
        # page_id == entity_id: одна страница на сущность
        text = file.read_text(encoding="utf-8")
        stats["wiki_sections"] += app.wiki.index_page(row.page_id, row.page_id, row.path, text)
        progress.advance()

    app.registry.set_meta("embedder", embedder_signature(app.embedder))
    log.info("reindex_done", **stats)
    return stats


def reset(app: App) -> None:
    """Удаляет все векторы, узлы графа, строки registry, файлы кэша и страницы wiki.

    Удаляются только файлы wiki, которые пишет mnogobase (`entities/`, `index.md`, `log.md`);
    сам каталог wiki удаляется, только если в нём больше ничего не осталось."""
    app.vectors.drop_collections()
    app.graph.wipe()
    app.registry.wipe()
    shutil.rmtree(app.settings.data_dir / "cache", ignore_errors=True)
    wiki_dir = app.settings.wiki.dir
    shutil.rmtree(wiki_dir / "entities", ignore_errors=True)
    for name in WIKI_FILES:
        (wiki_dir / name).unlink(missing_ok=True)
    with contextlib.suppress(OSError):  # его нет, или в нём чужие для mnogobase файлы
        wiki_dir.rmdir()
    get_logger(__name__).info("reset_done")
