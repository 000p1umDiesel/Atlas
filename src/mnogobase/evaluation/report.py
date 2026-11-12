"""Offline, completeness-aware CSV and Markdown reports."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from statistics import mean

from mnogobase.files import fingerprint

from .artifacts import latest_records
from .comparison import comparison_reports
from .core import planned_answers
from .ragas import METRIC_NAMES

STATUSES = ("ok", "skipped", "error", "undefined", "missing")
LABELS = {"rag": "Hybrid RAG", "wiki": "WikiLLM", "graph": "GraphRAG", "all": "Combined"}


def _csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def report(output: Path) -> list[dict]:
    manifest, answers = planned_answers(output)
    judge_path = output / "judge.json"
    if judge_path.exists():
        judge = json.loads(judge_path.read_text(encoding="utf-8"))
        if (
            judge.get("fingerprint") != fingerprint(judge["config"])
            or judge["config"]["run_fingerprint"] != manifest["fingerprint"]
        ):
            raise ValueError("Judge manifest does not match this run")
    scores = (
        latest_records(output / "scores.jsonl", ("id", "mode", "metric"))
        if judge_path.exists()
        else {}
    )
    details = []
    summary = []
    for row in answers:
        for name in METRIC_NAMES:
            score = scores.get((row["id"], row["mode"], name), {})
            if score.get("answer_fingerprint") != fingerprint(row):
                score = {}
            details.append(
                {
                    "id": row["id"],
                    "mode": row["mode"],
                    "question": row["question"],
                    "metric": name,
                    "status": score.get("status", "missing"),
                    "value": score.get("value"),
                    "detail": score.get("reason", score.get("error", "")),
                    "answer_status": row["status"],
                    "latency_ms": row.get("latency_ms"),
                    "tokens_in": row.get("tokens_in"),
                    "tokens_out": row.get("tokens_out"),
                    "context_tokens": row.get("context_tokens"),
                }
            )
    for mode in manifest["config"]["modes"]:
        mode_answers = [r for r in answers if r["mode"] == mode]
        ok_answers = [r for r in mode_answers if r["status"] == "ok"]
        for name in METRIC_NAMES:
            part = [r for r in details if r["mode"] == mode and r["metric"] == name]
            values = [r["value"] for r in part if r["status"] == "ok"]
            summary.append(
                {
                    "mode": mode,
                    "approach": LABELS[mode],
                    "metric": name,
                    "mean": mean(values) if values else None,
                    "planned": len(part),
                    **{s: sum(r["status"] == s for r in part) for s in STATUSES},
                    **{
                        f"answers_{s}": sum(r["status"] == s for r in mode_answers)
                        for s in ("ok", "error", "missing")
                    },
                    "latency_ms_mean": mean(r.get("latency_ms", 0) for r in ok_answers)
                    if ok_answers
                    else None,
                    "tokens_in_total": sum(r.get("tokens_in", 0) for r in ok_answers),
                    "tokens_out_total": sum(r.get("tokens_out", 0) for r in ok_answers),
                }
            )
    _csv(output / "details.csv", details)
    _csv(output / "summary.csv", summary)
    lines = [
        "# Retrieval evaluation",
        "",
        "Means include successful metric values only; skipped/error/missing are not zero scores.",
        "",
        "| Approach | Metric | Mean | OK / planned | Skipped | Errors | Undefined | Missing |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for s in summary:
        value = f"{s['mean']:.4f}" if s["mean"] is not None else "—"
        lines.append(
            f"| {s['approach']} | {s['metric']} | {value} | {s['ok']}/{s['planned']} | {s['skipped']} | {s['error']} | {s['undefined']} | {s['missing']} |"
        )
    lines.extend(
        [
            "",
            "| Approach | Answers OK / planned | Failed | Missing | Mean latency, ms | Tokens in / out |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for mode in manifest["config"]["modes"]:
        s = next(r for r in summary if r["mode"] == mode)
        latency = f"{s['latency_ms_mean']:.1f}" if s["latency_ms_mean"] is not None else "—"
        lines.append(
            f"| {s['approach']} | {s['answers_ok']}/{s['planned']} | {s['answers_error']} | {s['answers_missing']} | {latency} | {s['tokens_in_total']} / {s['tokens_out_total']} |"
        )
    pending = sum(s["error"] + s["undefined"] + s["missing"] for s in summary)
    lines.extend(
        [
            "",
            f"Remaining/failed metric pairs: {pending}.",
            "",
            "Latency covers translation, retrieval and generation. Token totals cover successful answers only, not judge calls or failed attempts.",
            "A skipped metric has no numeric value; inspect details.csv for the reason.",
        ]
    )
    lines.extend(comparison_reports(output, manifest, answers, details))
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary
