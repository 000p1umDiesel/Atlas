"""Офлайн-аналитика графа (`graph analyze | status | clear`): взвешенный PageRank и
сообщества Leiden через Neo4j GDS, без вызовов моделей.

`analyze` и `clear` пишут в граф, поэтому берут блокировки `common.graph_writer`
(graph-analysis, затем корпус): анализ не идёт одновременно ни с другим анализом, ни с
ingest или worker, которые меняют граф под ним. `status` только читает.

Результат анализа — снимок в узлах графа (оценки PageRank, номера сообществ). `ask` никогда
не запускает GDS сам, а только читает этот снимок при `graph.use_gds`; после изменения
корпуса снимок устаревает до следующего `graph analyze`.
"""

from __future__ import annotations

from typing import Annotated

import typer
from neo4j.exceptions import Neo4jError, ServiceUnavailable, SessionExpired

from mnogobase.cli import common
from mnogobase.ops.graph_analysis import AnalysisError
from mnogobase.ops.graph_analysis import analyze as analyze_graph
from mnogobase.stores.graph_store import GraphStore

app = typer.Typer(no_args_is_help=True, help="Graph analysis with APOC/GDS.")
common.app.add_typer(app, name="graph")

# Ошибки Neo4j и некорректных данных графа: печатаются коротко с кодом 2, без traceback.
_GRAPH_ERRORS = (Neo4jError, ServiceUnavailable, SessionExpired, ValueError)


@app.command("analyze")
def graph_analyze(
    estimate: Annotated[bool, typer.Option("--estimate", help="Estimate projection only.")] = False,
) -> None:
    """Посчитать взвешенный PageRank и сообщества Leiden (GDS) без вызовов моделей.

    Результат записывается в граф как снимок, который ask использует при graph.use_gds и
    graph.community. С --estimate только оценивает память проекции и ничего не пишет.
    Размер графа ограничен настройками graph.analysis. Печатает отчёт в JSON. Код выхода 2,
    если Neo4j или GDS недоступны, граф превышает лимиты, изменился во время анализа или
    уже идёт другой анализ либо запись корпуса."""
    settings = common.get_settings(project=True)
    common.preflight(settings, ("neo4j",))
    graph = GraphStore.from_settings(settings.neo4j)
    try:
        with common.graph_writer(settings):
            graph.ensure_schema()
            if not estimate:
                # проекции, оставшиеся от прерванного анализа, занимают память GDS
                for name in graph.owned_projections():
                    graph.drop_projection(name)
            report = analyze_graph(graph, settings.graph.analysis, estimate_only=estimate)
            common.console.print_json(data=report)
    except (AnalysisError, *_GRAPH_ERRORS) as exc:
        common.fail(exc)
    finally:
        graph.close()


@app.command("status")
def graph_status() -> None:
    """Показать свежесть снимка аналитики и самые центральные сущности.

    Печатает в JSON, готов ли снимок, каким запуском и когда он посчитан, и до 20
    сущностей с наибольшим PageRank вместе с их сообществами. Ничего не меняет."""
    settings = common.get_settings(project=True)
    common.preflight(settings, ("neo4j",))
    graph = GraphStore.from_settings(settings.neo4j)
    try:
        common.console.print_json(
            data={**graph.analysis_status(), "entities": graph.analysis_details()}
        )
    except _GRAPH_ERRORS as exc:
        common.fail(exc)
    finally:
        graph.close()


@app.command("clear")
def graph_clear(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation.")] = False,
) -> None:
    """Удалить результаты аналитики графа, сохранив сами знания.

    Стираются только вычисленные оценки (PageRank, сообщества) и временные проекции GDS,
    созданные mnogobase; документы, сущности и связи остаются. Без --yes спрашивает
    подтверждение."""
    settings = common.get_settings(project=True)
    if not yes:
        typer.confirm(
            "Clear graph analytics? Source documents and knowledge are preserved.", abort=True
        )
    common.preflight(settings, ("neo4j",))
    graph = GraphStore.from_settings(settings.neo4j)
    try:
        with common.graph_writer(settings):
            graph.clear_analysis()
            if graph.plugin_status()["gds"]:
                for name in graph.owned_projections():
                    graph.drop_projection(name)
        common.console.print("Graph analytics cleared.")
    except _GRAPH_ERRORS as exc:
        common.fail(exc)
    finally:
        graph.close()
