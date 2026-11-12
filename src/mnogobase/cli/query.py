"""Вопросы к базе знаний: `ask` (один режим) и `compare` (все режимы рядом).

Чтение не берёт блокировок: отвечать можно параллельно с ingest и фоновым worker.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from mnogobase.cli import common, render
from mnogobase.retrieval import Mode
from mnogobase.retrieval.compare import compare as do_compare
from mnogobase.retrieval.compare import config_hash, run_mode

app = common.app


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="Question to answer from the knowledge base.")],
    mode: Annotated[Mode, typer.Option("--mode", "-m", help="rag | wiki | graph | all")] = Mode.ALL,
    k: Annotated[int | None, typer.Option("--k", min=1, help="Results per retriever.")] = None,
) -> None:
    """Ответить на вопрос со ссылками на источники в одном режиме retrieval."""
    settings = common.get_settings(project=True)
    common.preflight(settings)
    application = common.build_app(settings)
    try:
        retrievers, answerer, translator = common.answer_setup(application)
        with common.model_errors():
            answer = asyncio.run(
                run_mode(
                    question,
                    mode.value,
                    retrievers[mode.value],
                    answerer,
                    k or settings.retrieval.k,
                    translator,
                )
            )
    finally:
        application.close()
    render.print_answer(answer)


@app.command()
def compare(
    question: Annotated[str, typer.Argument(help="Question to answer in all four modes.")],
    k: Annotated[int | None, typer.Option("--k", min=1, help="Results per retriever.")] = None,
) -> None:
    """Ответить в режимах rag / wiki / graph / all и записать сравнение в runs/compare.jsonl."""
    settings = common.get_settings(project=True)
    common.preflight(settings)
    application = common.build_app(settings)
    try:
        retrievers, answerer, translator = common.answer_setup(application)
        with common.model_errors():
            answers = asyncio.run(
                do_compare(
                    question,
                    retrievers,
                    answerer,
                    k or settings.retrieval.k,
                    settings.runs_dir,
                    config_hash(settings),
                    translator,
                )
            )
    finally:
        application.close()
    table = Table("mode", "answer", "sources (cited)", "latency ms", "tokens in/out", "context")
    for a in answers:
        cited = (
            ", ".join(render.source_label(x.path, x.page, x.ref) for x in a.sources if x.cited)
            or "-"
        )
        table.add_row(
            a.mode,
            escape(a.text),
            escape(cited),
            str(a.latency_ms),
            f"{a.tokens_in}/{a.tokens_out}",
            str(a.context_tokens),
        )
    common.console.print(table)
