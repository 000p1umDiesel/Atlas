"""Офлайн-анализ графа в GDS: `mnogobase graph analyze`.

Проецирует сущности (`Entity`) и связи `RELATED` (неориентированно, с весом `weight`) в
память GDS, считает взвешенный PageRank и сообщества Leiden и атомарно публикует снимок в
граф (`GraphStore.publish_analysis`). Модели не вызываются. `ask` GDS никогда не запускает:
он только читает опубликованный снимок (`graph.use_gds`, `graph.community`).

Вызывает CLI (`cli/graph.py`), держа advisory-locks `graph-analysis` и корпуса
(`graph_writer`), так что граф во время анализа никто не пишет; ревизия `AnalysisState`
всё равно сверяется при публикации. Размер графа и память ограничены
`graph.analysis` (`max_nodes`, `max_relationships`, `memory_mb`, `timeout_s`); всё, что
выходит за пределы, — `AnalysisError` до тяжёлой работы.
"""

from __future__ import annotations

import hashlib
import json
import sys
import uuid
from collections import defaultdict

from neo4j.exceptions import Neo4jError

from mnogobase.core.config import GraphAnalysisSettings
from mnogobase.core.log import get_logger
from mnogobase.stores.graph_store import PROJECTION_PREFIX, GraphStore


class AnalysisError(RuntimeError):
    """Анализ невозможен или его результат нельзя публиковать (нет GDS, граф больше
    лимитов, не хватает памяти, граф изменился по ходу); сообщение безопасно показывать."""


def check_memory(projection: int, workspace: int, *, budget_mb: int) -> None:
    """Бросает `AnalysisError`, если проекция плюс рабочая память алгоритма (байты)
    превышают бюджет `budget_mb` МиБ."""
    if projection + workspace > budget_mb * 1024 * 1024:
        raise AnalysisError(
            f"Оценка памяти GDS ({projection + workspace} байт) превышает бюджет {budget_mb} МиБ"
        )


def community_keys(rows: list[dict]) -> dict[str, str]:
    """Стабильные ключи сообществ: `entity_id` → `community:<sha256 состава>[:24]`.

    Номера сообществ GDS произвольны и меняются от запуска к запуску; ключ из
    отсортированного списка участников одинаков для одинакового состава, так что повторный
    анализ неизменного графа даёт те же ключи. `rows` — строки
    `{entity_id, community_id}`."""
    groups: dict[int | str, list[str]] = defaultdict(list)
    for row in rows:
        groups[row["community_id"]].append(row["entity_id"])
    result = {}
    for members in groups.values():
        payload = json.dumps(sorted(members), ensure_ascii=False, separators=(",", ":"))
        key = "community:" + hashlib.sha256(payload.encode()).hexdigest()[:24]
        result.update(dict.fromkeys(members, key))
    return result


def analyze(
    graph: GraphStore,
    settings: GraphAnalysisSettings,
    *,
    estimate_only: bool = False,
) -> dict:
    """Проверяет граф, считает PageRank + Leiden и публикует снимок; возвращает отчёт.

    Шаги:
    1. Нужны процедуры GDS, схема графа (ревизия `AnalysisState`), размер в пределах
       `max_nodes` / `max_relationships` и положительные конечные веса `RELATED`.
    2. Оценка памяти проекции против `memory_mb`. С `estimate_only` здесь возврат, ничего
       не создаётся и не пишется.
    3. Пустой граф публикует пустой снимок.
    4. Проекция с уникальным именем (`PROJECTION_PREFIX` + uuid), оценка памяти алгоритмов,
       PageRank, Leiden (для графа без рёбер каждая сущность — своё сообщество),
       проверка, что оба алгоритма вернули одно множество сущностей.
    5. `publish_analysis` пишет снимок, только если ревизия не изменилась.
    6. Проекция удаляется всегда (`finally`); ошибка удаления логируется, а
       пробрасывается, только если сам анализ прошёл успешно, чтобы не скрыть исходную.

    Отчёт: счётчики графа, ревизия, версии плагинов, настройки, `projection_version`,
    число сообществ и оценки памяти. Исключения: `AnalysisError`, ошибки Neo4j."""
    plugins = graph.plugin_status()
    missing = [name for name in plugins["missing"] if name.startswith("gds.")]
    if not plugins["gds"] or missing:
        raise AnalysisError(f"Процедуры GDS недоступны: {', '.join(missing) or 'gds.version'}")
    state = graph.analysis_status()
    if state["revision"] is None:
        raise AnalysisError("Схемы графа нет: сначала инициализируйте проект")
    counts = graph.analysis_counts()
    if counts["nodes"] > settings.max_nodes:
        raise AnalysisError(f"Узлов в графе: {counts['nodes']}, лимит {settings.max_nodes}")
    if counts["relationships"] > settings.max_relationships:
        raise AnalysisError(
            f"Связей в графе: {counts['relationships']}, лимит {settings.max_relationships}"
        )
    if counts["invalid_weights"]:
        raise AnalysisError("Веса RELATED должны быть положительными конечными числами")
    report = {
        **counts,
        "revision": state["revision"],
        "plugins": plugins,
        "settings": settings.model_dump(),
        "projection_version": "entity-related-undirected-v1",
        "communities": 0,
        "projection_bytes": 0,
        "algorithm_bytes": 0,
        "estimate_only": estimate_only,
    }
    if counts["nodes"]:
        report["projection_bytes"] = graph.gds_project_estimate(settings.timeout_s)
        check_memory(report["projection_bytes"], 0, budget_mb=settings.memory_mb)
    if estimate_only:
        return report
    if not counts["nodes"]:
        if not graph.publish_analysis(state["revision"], [], report):
            raise AnalysisError(
                "Граф изменился во время анализа: запустите `mnogobase graph analyze` снова"
            )
        return report
    name = f"{PROJECTION_PREFIX}{uuid.uuid4().hex}"
    try:
        graph.gds_project(name, settings.timeout_s)
        # Алгоритмы идут по очереди: с проекцией одновременно живёт только большая из их
        # рабочих памятей.
        report["algorithm_bytes"] = max(
            graph.gds_algorithm_estimate(name, algorithm, settings.timeout_s)
            for algorithm in ("pageRank", "leiden")
        )
        check_memory(
            report["projection_bytes"], report["algorithm_bytes"], budget_mb=settings.memory_mb
        )
        ranks = graph.gds_pagerank(name, settings.timeout_s)
        if counts["relationships"]:
            communities = community_keys(graph.gds_leiden(name, settings.timeout_s))
        else:
            # Leiden в GDS 2.13 падает на графе без рёбер: каждый узел — своё сообщество.
            communities = community_keys(
                [{"entity_id": row["entity_id"], "community_id": row["entity_id"]} for row in ranks]
            )
        if set(communities) != {row["entity_id"] for row in ranks}:
            raise AnalysisError("Алгоритмы GDS вернули разные наборы сущностей")
        report["communities"] = len(set(communities.values()))
        rows = [{**row, "community": communities[row["entity_id"]]} for row in ranks]
        if not graph.publish_analysis(state["revision"], rows, report):
            raise AnalysisError(
                "Граф изменился во время анализа: запустите `mnogobase graph analyze` снова"
            )
        return report
    finally:
        # летит ли уже исключение: тогда ошибка очистки его не заменяет
        failed = sys.exc_info()[0] is not None
        try:
            graph.drop_projection(name)
        except Neo4jError:
            get_logger(__name__).error("gds_projection_cleanup_failed", graph=name)
            if not failed:
                raise
