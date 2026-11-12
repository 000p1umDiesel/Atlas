"""Paired comparisons, category breakdowns and honestly scoped preprocessing costs."""

from __future__ import annotations

import csv
import random
from itertools import combinations
from pathlib import Path
from statistics import mean

from mnogobase.core.files import read_jsonl

from .ragas import METRIC_NAMES


def _csv(path, rows, fields):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _interval(deltas):
    if len(deltas) < 2:
        return None, None
    rng = random.Random(159)
    means = sorted(mean(rng.choices(deltas, k=len(deltas))) for _ in range(1000))
    return means[24], means[974]


def _fmt(value):
    return "—" if value is None else f"{value:.4f}"


def comparison_reports(output: Path, manifest: dict, answers: list, details: list) -> list[str]:
    modes = manifest["config"]["modes"]
    by_key = {(r["id"], r["mode"], r["metric"]): r for r in details}
    paired = []
    lines = [
        "",
        "## Paired comparisons",
        "",
        "Differences use exactly the same successfully scored questions in both modes.",
        "Positive delta means mode B has a higher metric value. Missing/error scores are excluded, not zero.",
        "CI is an exploratory 95% paired bootstrap interval (1000 resamples); small samples do not establish a winner.",
        "",
        "| Mode A | Mode B | Metric | Paired N | Mean B - A | CI low | CI high |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    ids = [q["id"] for q in manifest["config"]["questions"]]
    for a, b in combinations(modes, 2):
        for metric in METRIC_NAMES:
            pairs = [
                (by_key[(qid, a, metric)], by_key[(qid, b, metric)])
                for qid in ids
                if by_key[(qid, a, metric)]["status"] == "ok"
                and by_key[(qid, b, metric)]["status"] == "ok"
            ]
            deltas = [right["value"] - left["value"] for left, right in pairs]
            low, high = _interval(deltas)
            delta = mean(deltas) if deltas else None
            paired.append(
                {
                    "mode_a": a,
                    "mode_b": b,
                    "metric": metric,
                    "paired_n": len(pairs),
                    "delta_b_minus_a": delta,
                    "ci_low": low,
                    "ci_high": high,
                }
            )
            lines.append(
                f"| {a} | {b} | {metric} | {len(pairs)} | {_fmt(delta)} | {_fmt(low)} | {_fmt(high)} |"
            )
    _csv(
        output / "paired.csv",
        paired,
        ["mode_a", "mode_b", "metric", "paired_n", "delta_b_minus_a", "ci_low", "ci_high"],
    )
    categories = {
        q["id"]: str(q.get("category") or "unclassified") for q in manifest["config"]["questions"]
    }
    breakdown = []
    for mode in modes:
        for category in sorted(set(categories.values())):
            for metric in METRIC_NAMES:
                rows = [
                    r
                    for r in details
                    if r["mode"] == mode
                    and r["metric"] == metric
                    and categories[r["id"]] == category
                ]
                values = [r["value"] for r in rows if r["status"] == "ok"]
                breakdown.append(
                    {
                        "mode": mode,
                        "category": category,
                        "metric": metric,
                        "planned": len(rows),
                        "ok": len(values),
                        "mean": mean(values) if values else None,
                        "not_scored": len(rows) - len(values),
                    }
                )
    _csv(
        output / "by_category.csv",
        breakdown,
        ["mode", "category", "metric", "planned", "ok", "mean", "not_scored"],
    )
    lines.extend(
        [
            "",
            "Category breakdown: by_category.csv. Paired differences: paired.csv.",
            "These compare the implemented retrieval modes. Wiki generation uses graph relations; this is not an independent graph-free wiki baseline.",
        ]
    )
    lines.extend(_processing_report(output, manifest))
    return lines


def _processing_report(output, manifest):
    rows = read_jsonl(output / "processing.jsonl", recover_tail=True)
    corpus = manifest["config"].get("corpus")
    if corpus and any(r["after"]["project"] != corpus["project"] for r in rows):
        raise ValueError("Processing history belongs to a different project")
    costs, stages = [], []
    for row in rows:
        costs.append(
            {
                key: row.get(key)
                for key in (
                    "id",
                    "phase",
                    "status",
                    "duration_ms",
                    "tokens_in",
                    "tokens_out",
                    "processed",
                    "skipped",
                    "failed",
                    "wiki_failed",
                    "scope",
                )
            }
        )
        for stage in row.get("stages", []):
            stages.append(
                {
                    "run_id": row["id"],
                    "phase": row["phase"],
                    **{
                        k: stage.get(k)
                        for k in (
                            "stage",
                            "duration_ms",
                            "tokens_in",
                            "tokens_out",
                            "completed",
                            "failed",
                        )
                    },
                }
            )
    _csv(
        output / "processing.csv",
        costs,
        [
            "id",
            "phase",
            "status",
            "duration_ms",
            "tokens_in",
            "tokens_out",
            "processed",
            "skipped",
            "failed",
            "wiki_failed",
            "scope",
        ],
    )
    _csv(
        output / "processing_stages.csv",
        stages,
        [
            "run_id",
            "phase",
            "stage",
            "duration_ms",
            "tokens_in",
            "tokens_out",
            "completed",
            "failed",
        ],
    )
    lines = [
        "",
        "## Processing cost",
        "",
        "Measured shared preprocessing, not independent per-architecture build cost.",
        "Main LLM successful calls only; excludes vision, embeddings, reranker, judge calls and failed network calls.",
        "Stage totals are a breakdown of run time; do not add them to the end-to-end run time.",
    ]
    if not rows:
        return [*lines, "No processing measurements attached. Use eval ingest before benchmarking."]
    lines.extend(
        [
            "",
            "| Phase | Attempts | Failed/interrupted | Total ms | LLM tokens in / out |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for phase in ("build", "update"):
        part = [r for r in rows if r["phase"] == phase]
        known = all(
            r.get("tokens_in") is not None and r.get("tokens_out") is not None for r in part
        )
        usage = (
            f"{sum(r['tokens_in'] for r in part)} / {sum(r['tokens_out'] for r in part)}"
            if known
            else "unavailable"
        )
        lines.append(
            f"| {phase} | {len(part)} | {sum(r['status'] != 'ok' for r in part)} | {sum(r['duration_ms'] for r in part)} | {usage} |"
        )
    if corpus and rows[-1]["after"]["fingerprint"] != corpus["fingerprint"]:
        lines.extend(
            [
                "",
                "WARNING: latest processing snapshot differs from the evaluated corpus; these costs do not fully describe that corpus.",
            ]
        )
    return lines
