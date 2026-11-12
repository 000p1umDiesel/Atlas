"""Вопросы к базе знаний: `ask` (один режим) и `compare` (все режимы рядом).

Чтение не берёт блокировок: отвечать можно параллельно с ingest и фоновым worker. Ответ
строится в два шага: retriever выбранного режима собирает контекст в пределах бюджета токенов,
затем `Answerer` просит LLM ответить по этому контексту со ссылками `[n]` на источники.
Вопрос не на английском может быть сначала переведён (`retrieval.translate`), чтобы искать и
по оригиналу, и по переводу.
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
    question: Annotated[str, typer.Argument(help="Вопрос к базе знаний")],
    mode: Annotated[Mode, typer.Option("--mode", "-m", help="rag | wiki | graph | all")] = Mode.ALL,
    k: Annotated[
        int | None, typer.Option("--k", min=1, help="Сколько результатов берёт каждый retriever")
    ] = None,
) -> None:
    """Ответить на вопрос со ссылками на источники в одном режиме retrieval.

    Режимы: rag (поиск по чанкам документов), wiki (страницы wiki), graph (обход графа
    знаний) и all (все вместе с общим бюджетом контекста). Только читает базу, поэтому
    работает параллельно с ingest и worker. Код выхода 2, если недоступен сервис, индекс
    построен другим эмбеддером или упал вызов модели."""
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
    question: Annotated[
        str, typer.Argument(help="Вопрос, на который ответить во всех четырёх режимах")
    ],
    k: Annotated[
        int | None, typer.Option("--k", min=1, help="Сколько результатов берёт каждый retriever")
    ] = None,
) -> None:
    """Ответить во всех режимах rag / wiki / graph / all и сравнить ответы.

    Печатает таблицу: ответ, процитированные источники, задержка, токены и размер
    контекста каждого режима. Каждый запуск дописывает запись в runs/compare.jsonl
    (вместе с хешем конфигурации), чтобы сравнения можно было сопоставить позже."""
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
    table = Table(
        "режим", "ответ", "источники (цит.)", "задержка, мс", "токены вход/выход", "контекст"
    )
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
