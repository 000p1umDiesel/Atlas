from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path
from typing import Annotated

import filelock
import typer
import yaml
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from mnogobase.app import App, build_app
from mnogobase.config import Settings, load_settings
from mnogobase.doctor import run_checks
from mnogobase.log import configure_logging, new_run_id
from mnogobase.maintenance import reindex as do_reindex
from mnogobase.maintenance import reset as do_reset
from mnogobase.models import Answer
from mnogobase.pipeline import EmbedderMismatchError
from mnogobase.registry import STAGES, Registry, ingest_lock
from mnogobase.retrieval import Mode, build_retrievers
from mnogobase.retrieval.answer import Answerer
from mnogobase.retrieval.base import Retriever
from mnogobase.retrieval.compare import compare as do_compare
from mnogobase.retrieval.compare import config_hash, run_mode
from mnogobase.stores.qdrant_store import DimensionMismatchError
from mnogobase.wiki.builder import WikiReport

app = typer.Typer(no_args_is_help=True, help="mnogobase — LLM Wiki core: ingest, wiki, retrieval.")
wiki_app = typer.Typer(no_args_is_help=True, help="Wiki commands.")
app.add_typer(wiki_app, name="wiki")
console = Console()
_state: dict[str, Path | None] = {"config": None}
# services a command needs before it starts: embedder, LLM, vector store, graph
PREFLIGHT_CHECKS = ("ollama", "llm", "qdrant", "neo4j")
REINDEX_CHECKS = ("ollama", "qdrant", "neo4j")
RESET_CHECKS = ("qdrant", "neo4j")


@app.callback()
def main(
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Path to config.yaml.")
    ] = None,
) -> None:
    _state["config"] = config


def _settings(*, project: bool = False, missing_exit: int = 2) -> Settings:
    """Load settings and start file logging. `project=True` first requires an existing
    project (else exit `missing_exit`), so a command run in the wrong directory creates
    neither state.db nor logs/."""
    try:
        settings = load_settings(_state["config"])
    except FileNotFoundError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2) from None
    except (yaml.YAMLError, ValueError) as exc:  # bad YAML, or a pydantic ValidationError
        console.print(f"[red]Invalid configuration:[/red] {escape(str(exc))}")
        raise typer.Exit(2) from None
    if project:
        _require_project(settings, missing_exit)
    configure_logging(settings.logs_dir)
    return settings


def _preflight(settings: Settings, checks: tuple[str, ...] = PREFLIGHT_CHECKS) -> None:
    """Stop early (exit 2) when a service the command depends on is unreachable."""
    failed = [c for c in run_checks(settings, only=checks) if not c.ok]
    if failed:
        for check in failed:
            console.print(f"[red]✗ {check.name}[/red]: {escape(check.detail)}")
        console.print("Run `mnogobase doctor` for a full report.")
        raise typer.Exit(2)


def _state_db(settings: Settings) -> Path:
    return settings.data_dir / "state.db"


def _require_project(settings: Settings, code: int = 2) -> None:
    """Exit with `code` outside a project: commands that only read must not create state.db."""
    if not _state_db(settings).is_file():
        console.print(
            f"No mnogobase project here (no {escape(str(_state_db(settings).resolve()))}). "
            "Run `mnogobase ingest` first, or pass the project's --config.",
            soft_wrap=True,
        )
        raise typer.Exit(code)


def _lock(settings: Settings) -> filelock.FileLock:
    lock = ingest_lock(settings.data_dir)
    try:
        lock.acquire()
    except filelock.Timeout:
        console.print("[red]Another ingest / wiki / reindex / reset run is in progress.[/red]")
        raise typer.Exit(2) from None
    return lock


def _fail_on_mismatch(exc: Exception) -> None:
    console.print(f"[red]{escape(str(exc))}[/red]")
    raise typer.Exit(2) from exc


def _where(path: str | None, page: int | None, ref: str) -> str:
    where = Path(path).name if path else ref
    return f"{where}, p.{page}" if page else where


def _print_answer(answer: Answer) -> None:
    # document and LLM text is printed verbatim: `[x]` in it is not Rich markup
    console.print(Panel(escape(answer.text), title=f"{answer.mode} · {answer.latency_ms} ms"))
    table = Table("#", "cited", "kind", "source", "ref", "snippet")
    for s in answer.sources:
        table.add_row(
            str(s.n),
            "✓" if s.cited else "",
            s.kind,
            escape(_where(s.path, s.page, s.ref)),
            escape(s.ref),
            escape(s.snippet[:80]),
        )
    console.print(table)


def _print_wiki_report(report: WikiReport) -> None:
    console.print(
        f"wiki: created {len(report.created)}, updated {len(report.updated)}, "
        f"deleted {len(report.deleted)}, skipped {len(report.skipped)}, "
        f"failed {len(report.failed)}"
    )
    for name in report.failed:
        console.print(f"[red]✗[/red] wiki page {escape(name)} (retried on the next build)")


@app.command()
def doctor() -> None:
    """Check device, Ollama, the LLM endpoint, Qdrant, Neo4j and the index signature."""
    checks = run_checks(_settings())
    table = Table("check", "status", "detail")
    for c in checks:
        table.add_row(c.name, "[green]ok[/green]" if c.ok else "[red]fail[/red]", escape(c.detail))
    console.print(table)
    if not all(c.ok for c in checks):
        raise typer.Exit(1)


@app.command()
def status() -> None:
    """Summarize ingested files, stage states, pending wiki updates and errors."""
    # status only reads: outside a project it says so and succeeds
    settings = _settings(project=True, missing_exit=0)
    registry = Registry(_state_db(settings))
    try:
        files = registry.files()
        counts = Counter(f.status for f in files)
        console.print(
            f"files: {len(files)} " + " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        )
        summary = registry.stage_summary()
        table = Table("stage", "done", "failed", "pending", "running")
        for stage in STAGES:
            row = summary.get(stage, {})
            table.add_row(
                stage, *(str(row.get(k, 0)) for k in ("done", "failed", "pending", "running"))
            )
        console.print(table)
        console.print(f"entities waiting for wiki update: {len(registry.dirty())}")
        errors = registry.stage_errors()
        if errors:
            err_table = Table("doc_id", "stage", "error")
            for doc_id, stage, error in errors:
                err_table.add_row(doc_id, stage, escape(error or ""))
            console.print(err_table)
    finally:
        registry.close()


@app.command()
def ingest(
    paths: Annotated[list[Path] | None, typer.Argument(help="Files or folders to ingest.")] = None,
    no_wiki: Annotated[bool, typer.Option("--no-wiki", help="Skip the wiki update.")] = False,
    retry_failed: Annotated[
        bool, typer.Option("--retry-failed", help="Re-run failed stages.")
    ] = False,
) -> None:
    """Parse, chunk, embed and extract documents into Qdrant + Neo4j, then update the wiki."""
    settings = _settings()
    _preflight(settings)
    lock = _lock(settings)
    try:
        application = build_app(settings)
        try:
            targets = list(paths or [])
            if not targets and retry_failed:
                targets = [Path(p) for p in application.registry.failed_paths()]
            if not targets:
                console.print("Nothing to ingest.")
                return
            run_id = new_run_id()
            with console.status("Ingesting…"):
                try:
                    report = asyncio.run(
                        application.pipeline.ingest(
                            targets,
                            build_wiki=not no_wiki,
                            retry_failed=retry_failed,
                            run_id=run_id,
                        )
                    )
                except (EmbedderMismatchError, DimensionMismatchError) as exc:
                    _fail_on_mismatch(exc)
        finally:
            application.close()
    finally:
        lock.release()
    console.print(
        f"run {run_id}: processed {len(report.processed)}, skipped {len(report.skipped)}, "
        f"failed {len(report.failed)}"
    )
    if report.wiki:
        _print_wiki_report(report.wiki)
    for path, error in report.failed.items():
        console.print(f"[red]✗[/red] {escape(path)}: {escape(error)}")
    if report.failed or (report.wiki and report.wiki.failed):
        raise typer.Exit(1)


@wiki_app.command("build")
def wiki_build(
    rebuild_all: Annotated[bool, typer.Option("--all", help="Regenerate every page.")] = False,
) -> None:
    """Regenerate wiki pages for entities with new mentions (or all with --all)."""
    settings = _settings()
    _preflight(settings)
    lock = _lock(settings)
    try:
        application = build_app(settings)
        try:
            try:
                application.pipeline.prepare(resume=False)
            except (EmbedderMismatchError, DimensionMismatchError) as exc:
                _fail_on_mismatch(exc)
            # an interrupted ingest may have left a document removal half done (orphan pages)
            deleted = application.pipeline.finish_pending_removals()
            report = asyncio.run(
                application.wiki.build(
                    rebuild_all=rebuild_all, run_id=new_run_id(), deleted=deleted
                )
            )
        finally:
            application.close()
    finally:
        lock.release()
    _print_wiki_report(report)
    if report.failed:
        raise typer.Exit(1)


def _answer_setup(application: App) -> tuple[dict[str, Retriever], Answerer]:
    try:
        application.pipeline.prepare(resume=False)
    except (EmbedderMismatchError, DimensionMismatchError) as exc:
        _fail_on_mismatch(exc)
    s = application.settings
    retrievers = build_retrievers(
        application.vectors, application.graph, application.embedder, application.sparse, s
    )
    return retrievers, Answerer(application.llm, s.retrieval.context_tokens)


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="Question to answer from the knowledge base.")],
    mode: Annotated[Mode, typer.Option("--mode", "-m", help="rag | wiki | graph | all")] = Mode.ALL,
    k: Annotated[int | None, typer.Option("--k", help="Results per retriever.")] = None,
) -> None:
    """Answer a question with citations using one retrieval mode."""
    settings = _settings(project=True)
    _preflight(settings)
    application = build_app(settings)
    try:
        retrievers, answerer = _answer_setup(application)
        answer = asyncio.run(
            run_mode(
                question, mode.value, retrievers[mode.value], answerer, k or settings.retrieval.k
            )
        )
    finally:
        application.close()
    _print_answer(answer)


@app.command()
def compare(
    question: Annotated[str, typer.Argument(help="Question to answer in all four modes.")],
    k: Annotated[int | None, typer.Option("--k", help="Results per retriever.")] = None,
) -> None:
    """Answer in rag / wiki / graph / all and log the comparison to runs/compare.jsonl."""
    settings = _settings(project=True)
    _preflight(settings)
    application = build_app(settings)
    try:
        retrievers, answerer = _answer_setup(application)
        answers = asyncio.run(
            do_compare(
                question,
                retrievers,
                answerer,
                k or settings.retrieval.k,
                settings.runs_dir,
                config_hash(settings),
            )
        )
    finally:
        application.close()
    table = Table("mode", "answer", "sources (cited)", "latency ms", "tokens in/out", "context")
    for a in answers:
        cited = ", ".join(_where(x.path, x.page, x.ref) for x in a.sources if x.cited) or "-"
        table.add_row(
            a.mode,
            escape(a.text),
            escape(cited),
            str(a.latency_ms),
            f"{a.tokens_in}/{a.tokens_out}",
            str(a.context_tokens),
        )
    console.print(table)


@app.command()
def reindex() -> None:
    """Recompute every vector after changing the embedder (graph and wiki are kept)."""
    settings = _settings()
    _preflight(settings, REINDEX_CHECKS)
    lock = _lock(settings)
    try:
        application = build_app(settings)
        try:
            with console.status("Re-embedding…"):
                stats = do_reindex(application)
        finally:
            application.close()
    finally:
        lock.release()
    console.print(
        f"reindexed: {stats['chunks']} chunks, {stats['entities']} entities, "
        f"{stats['wiki_sections']} wiki sections"
    )


def _reset_targets(settings: Settings) -> list[str]:
    wiki_dir = settings.wiki.dir.resolve()
    return [
        f"Neo4j {settings.neo4j.uri}: all nodes in the default database",
        f"Qdrant {settings.qdrant.url}: collections with prefix {settings.qdrant.prefix!r}",
        f"state and caches in {settings.data_dir.resolve()}",
        f"wiki pages in {wiki_dir}: entities/, index.md, log.md",
    ]


@app.command()
def reset(
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask for confirmation.")] = False,
) -> None:
    """Delete ALL vectors, graph data, registry state, caches and wiki pages."""
    settings = _settings()
    state_db = _state_db(settings)
    if not state_db.is_file():
        # --yes never bypasses this: a wrong CWD or config must not wipe someone else's data
        console.print(
            f"[red]No mnogobase project here: {escape(str(state_db.resolve()))} does not exist.[/red] "
            "Run reset from the project directory or pass its --config.",
            soft_wrap=True,
        )
        raise typer.Exit(2)
    _preflight(settings, RESET_CHECKS)
    console.print("This deletes:", soft_wrap=True)
    for target in _reset_targets(settings):
        console.print(f"  - {escape(target)}", soft_wrap=True)
    if not yes and not typer.confirm("Delete all of the above?"):
        raise typer.Exit(1)
    lock = _lock(settings)
    try:
        application = build_app(settings)
        try:
            do_reset(application)
        finally:
            application.close()
    finally:
        lock.release()
    console.print("All data deleted.")
