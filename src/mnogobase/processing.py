"""Measured shared preprocessing and a local indexed-corpus identity.

The snapshot describes local indexed sources/wiki, not a transaction over external
databases. Writers must hold ingest.lock while taking it and generating answers.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from mnogobase.config import Settings
from mnogobase.database import DatabaseTarget, connect, database_target, target_exists
from mnogobase.evaluation.artifacts import append_jsonl, fingerprint
from mnogobase.ids import file_doc_id
from mnogobase.progress import NULL_PROGRESS, NullProgress

TOKEN_SCOPE = (
    "Main LLM successful calls only; excludes vision, embeddings, reranker and failed calls."
)


def corpus_snapshot(settings: Settings) -> dict:
    db = database_target(settings)
    files, meta, stages, removals, dirty = [], [], [], [], []
    if target_exists(db):
        connection = connect(db)
        try:
            files = connection.execute(
                "SELECT path,doc_id,status FROM files ORDER BY path"
            ).fetchall()
            meta = connection.execute("SELECT key,value FROM meta ORDER BY key").fetchall()
            stages = connection.execute(
                "SELECT doc_id,stage,status,attempts FROM stages ORDER BY doc_id,stage"
            ).fetchall()
            removals = connection.execute(
                "SELECT doc_id,plan_json FROM pending_removals ORDER BY doc_id"
            ).fetchall()
            dirty = connection.execute(
                "SELECT entity_id,marked_at FROM dirty_entities ORDER BY entity_id"
            ).fetchall()
        finally:
            connection.close()
    wiki = [
        (p.relative_to(settings.wiki.dir).as_posix(), hashlib.sha256(p.read_bytes()).hexdigest())
        for p in sorted(settings.wiki.dir.rglob("*.md"))
        if ".obsidian" not in p.parts
    ]
    project = fingerprint(
        {
            "state": str(db) if isinstance(db, DatabaseTarget) else str(db.resolve()),
            "qdrant": settings.qdrant.model_dump(),
            "neo4j": {"uri": settings.neo4j.uri, "user": settings.neo4j.user},
        }
    )
    return {
        "project": project,
        "fingerprint": fingerprint(
            {
                "files": files,
                "meta": meta,
                "wiki": wiki,
                "stages": stages,
                "removals": removals,
                "dirty": dirty,
            }
        ),
        "documents": len({r[1] for r in files}),
        "wiki_pages": len(wiki),
    }


class Measurement(NullProgress):
    def __init__(self, delegate, llm=None, *, clock=time.perf_counter):
        self.delegate, self.llm, self.clock = delegate, llm, clock
        self.started = clock()
        self.initial = self._usage()
        self.active = None
        self.stages = []

    def _usage(self):
        usage = getattr(self.llm, "usage", None)
        return (usage.tokens_in, usage.tokens_out) if usage is not None else (None, None)

    @staticmethod
    def _delta(before, after):
        return [
            b - a if a is not None and b is not None else None
            for a, b in zip(before, after, strict=True)
        ]

    def _close(self):
        if self.active is not None:
            stage, started, usage = self.active
            stage["duration_ms"] = max(0, round((self.clock() - started) * 1000))
            stage["tokens_in"], stage["tokens_out"] = self._delta(usage, self._usage())
            self.stages.append(stage)
            self.active = None

    def files_found(self, total):
        self.delegate.files_found(total)

    def file_started(self, path):
        self._close()
        self.delegate.file_started(path)

    def file_done(self, status):
        self._close()
        self.delegate.file_done(status)

    def step(self, name, total=None):
        if self.active is None or self.active[0]["stage"] != name:
            self._close()
            self.active = (
                {"stage": name, "completed": 0, "failed": 0, "total": total},
                self.clock(),
                self._usage(),
            )
        else:
            self.active[0]["total"] = total
        self.delegate.step(name, total)

    def advance(self, n=1, *, failed=False):
        if self.active:
            self.active[0]["completed"] += n
            self.active[0]["failed"] += n if failed else 0
        self.delegate.advance(n, failed=failed)

    def finish(self):
        self._close()
        tokens_in, tokens_out = self._delta(self.initial, self._usage())
        return {
            "duration_ms": max(0, round((self.clock() - self.started) * 1000)),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "stages": self.stages,
        }


async def measured_ingest(
    application,
    paths,
    *,
    phase=None,
    output: Path | None = None,
    settings: Settings | None = None,
    progress=NULL_PROGRESS,
    verify_sources=False,
    expected=None,
    **options,
):
    if phase is not None and phase not in ("build", "update"):
        raise ValueError("phase must be build or update")
    settings = settings or application.settings
    output = output or settings.data_dir / "processing.jsonl"
    before = corpus_snapshot(settings)
    phase = phase or ("build" if before["documents"] == 0 else "update")
    measurement = Measurement(progress, getattr(application, "llm", None))
    row = {
        "id": uuid.uuid4().hex,
        "ts": datetime.now(UTC).isoformat(),
        "phase": phase,
        "scope": "shared preprocessing for implemented retrieval modes",
        "token_scope": TOKEN_SCOPE,
        "before": before,
        "status": "interrupted",
    }
    report = None
    try:
        signatures = {}
        if verify_sources:
            for source in paths:
                source = Path(source).resolve()
                signatures[str(source)] = _source_signature(source)
                if expected and expected.get(str(source)) != signatures[str(source)][-1]:
                    raise SourceChangedError("Source changed after the job was claimed")
        report = await application.pipeline.ingest(paths, progress=measurement, **options)
        row["status"] = "error" if report.failed or (report.wiki and report.wiki.failed) else "ok"
        if verify_sources and row["status"] == "ok":
            acknowledged = {str(Path(p).resolve()) for p in [*report.processed, *report.skipped]}
            for path, signature in signatures.items():
                if path not in acknowledged:
                    raise SourceChangedError("Source was not processed or skipped by the pipeline")
                if _source_signature(Path(path)) != signature:
                    raise SourceChangedError("Source changed during processing; retry required")
        return report
    except Exception as exc:
        row.update(status="error", error=type(exc).__name__)
        raise
    finally:
        row.update(measurement.finish())
        if report is not None:
            row.update(
                processed=len(getattr(report, "processed", [])),
                skipped=len(getattr(report, "skipped", [])),
                failed=len(report.failed),
                wiki_failed=len(report.wiki.failed) if report.wiki else 0,
            )
        row["after"] = corpus_snapshot(settings)
        append_jsonl(output, row)


class SourceChangedError(RuntimeError):
    pass


def _source_signature(path: Path):
    before = path.stat()
    digest = file_doc_id(path)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ino,
    ):
        raise SourceChangedError("Source changed while hashing")
    return after.st_size, after.st_mtime_ns, after.st_ino, digest
