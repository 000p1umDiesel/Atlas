"""Bounded offline GDS analysis. Caller holds global analysis and corpus writer locks."""

from __future__ import annotations

import hashlib
import json
import sys
import uuid
from collections import defaultdict

from neo4j.exceptions import Neo4jError

from mnogobase.config import GraphAnalysisSettings
from mnogobase.log import get_logger
from mnogobase.stores.graph_store import GraphStore


class AnalysisError(RuntimeError):
    pass


def check_memory(projection: int, workspace: int, *, budget_mb: int) -> None:
    if projection + workspace > budget_mb * 1024 * 1024:
        raise AnalysisError(
            f"GDS memory estimate {projection + workspace} bytes exceeds {budget_mb} MiB budget"
        )


def community_keys(rows: list[dict]) -> dict[str, str]:
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
    plugins = graph.plugin_status()
    missing = [name for name in plugins["missing"] if name.startswith("gds.")]
    if not plugins["gds"] or missing:
        raise AnalysisError(
            f"GDS procedures are unavailable: {', '.join(missing) or 'gds.version'}"
        )
    state = graph.analysis_status()
    if state["revision"] is None:
        raise AnalysisError("Graph schema is missing: initialize the project first")
    counts = graph.analysis_counts()
    if counts["nodes"] > settings.max_nodes:
        raise AnalysisError(f"Graph has {counts['nodes']} nodes; limit is {settings.max_nodes}")
    if counts["relationships"] > settings.max_relationships:
        raise AnalysisError(
            f"Graph has {counts['relationships']} relationships; limit is {settings.max_relationships}"
        )
    if counts["invalid_weights"]:
        raise AnalysisError("RELATED weights must be positive finite numbers")
    report = {
        **counts,
        "scope": graph.analysis_scope,
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
            raise AnalysisError("Graph changed during analysis; rerun graph analyze")
        return report
    name = f"mb_analysis_{graph.analysis_scope}_{uuid.uuid4().hex}"
    try:
        graph.gds_project(name, settings.timeout_s)
        # The algorithms run sequentially; only the larger workspace overlaps the projection.
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
            # GDS 2.13 Leiden throws for an edgeless graph. Each isolated node is its own group.
            communities = community_keys(
                [{"entity_id": row["entity_id"], "community_id": row["entity_id"]} for row in ranks]
            )
        if set(communities) != {row["entity_id"] for row in ranks}:
            raise AnalysisError("GDS algorithms returned different entity sets")
        report["communities"] = len(set(communities.values()))
        rows = [{**row, "community": communities[row["entity_id"]]} for row in ranks]
        if not graph.publish_analysis(state["revision"], rows, report):
            raise AnalysisError("Graph changed during analysis; rerun graph analyze")
        return report
    finally:
        failed = sys.exc_info()[0] is not None
        try:
            graph.drop_projection(name)
        except Neo4jError:
            get_logger(__name__).error("gds_projection_cleanup_failed", graph=name)
            if not failed:
                raise
