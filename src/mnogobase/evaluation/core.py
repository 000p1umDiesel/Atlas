"""Inference and scoring against an immutable question × mode plan."""

from __future__ import annotations

import json
from importlib.metadata import version
from pathlib import Path

from mnogobase.config import Settings
from mnogobase.llm.templates import render
from mnogobase.models import Answer
from mnogobase.retrieval.compare import run_mode

from .artifacts import append_jsonl, ensure_manifest, fingerprint, latest_records, load_questions
from .ragas import METRIC_NAMES, RagasJudge, evaluate_one, skip_reason

MODES = ("rag", "wiki", "graph", "all")


def prepare_run(
    dataset: Path,
    output: Path,
    settings: Settings,
    *,
    modes=None,
    limit: int | None = None,
    k: int | None = None,
    corpus: dict | None = None,
) -> dict:
    questions = load_questions(dataset)
    modes = list(("rag", "wiki", "graph") if modes is None else modes)
    if not modes or len(set(modes)) != len(modes) or set(modes) - set(MODES):
        raise ValueError("Choose distinct modes: rag, wiki, graph, all")
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    if k is not None and k < 1:
        raise ValueError("k must be positive")
    snapshot = settings.model_dump(mode="json", exclude={"evaluation", "logs_dir", "runs_dir"})
    config = {
        "implementation": 1,
        "dataset_fingerprint": fingerprint(questions),
        "questions": questions[:limit],
        "modes": modes,
        "settings": snapshot,
        "k": k if k is not None else settings.retrieval.k,
        "answer_prompt": render(
            "answer", question="$question", context="$context", language="$language"
        ),
    }
    if corpus is not None:
        config["corpus"] = corpus
    return ensure_manifest(output / "run.json", config)


def load_run(output: Path) -> dict:
    path = output / "run.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("fingerprint") != fingerprint(manifest["config"]):
        raise ValueError(f"{path}: invalid manifest fingerprint")
    return manifest


def planned_answers(output: Path):
    manifest = load_run(output)
    latest = latest_records(output / "answers.jsonl", ("id", "mode"))
    planned = []
    for sample in manifest["config"]["questions"]:
        for mode in manifest["config"]["modes"]:
            row = latest.get((sample["id"], mode), {**sample, "mode": mode, "status": "missing"})
            if row.get("question") != sample["question"] or row.get("reference") != sample.get(
                "reference"
            ):
                raise ValueError(f"Answer does not match dataset: {sample['id']} / {mode}")
            if row["status"] == "ok":
                answer = Answer.model_validate(row)
                if not answer.text.strip():
                    raise ValueError(f"Empty answer: {sample['id']} / {mode}")
                if "contexts" not in row or not all(isinstance(c, str) for c in row["contexts"]):
                    raise ValueError(f"Missing full contexts: {sample['id']} / {mode}")
            planned.append(row)
    return manifest, planned


def _notify(progress, event: str, **fields):
    if progress is not None:
        progress(event, **fields)


async def run_answers(
    output: Path, retrievers: dict, answerer, translator=None, *, progress=None
) -> int:
    manifest, planned = planned_answers(output)
    work = [row for row in planned if row["status"] != "ok"]
    _notify(progress, "start", total=len(planned), remaining=len(work))
    samples = {q["id"]: q for q in manifest["config"]["questions"]}
    for row in work:
        sample = samples[row["id"]]
        mode = row["mode"]
        _notify(progress, "item", label=f"{sample['id']} / {mode}")
        try:
            answer = await run_mode(
                sample["question"],
                mode,
                retrievers[mode],
                answerer,
                manifest["config"]["k"],
                translator,
            )
            if not answer.text.strip():
                raise ValueError("Model returned an empty answer")
            result = {**sample, **answer.model_dump(mode="json"), "status": "ok"}
        except Exception as exc:
            result = {**sample, "mode": mode, "status": "error", "error": type(exc).__name__}
        append_jsonl(output / "answers.jsonl", result)
        _notify(
            progress,
            "done",
            status=result["status"],
            label=f"{sample['id']} / {mode}",
            detail=result.get("error", ""),
        )
    _, current = planned_answers(output)
    return sum(row["status"] != "ok" for row in current)


async def score_run(output: Path, judge: dict, *, names=None, metrics=None, progress=None) -> int:
    names = list(METRIC_NAMES if names is None else names)
    if not names or len(set(names)) != len(names) or set(names) - set(METRIC_NAMES):
        raise ValueError(f"Choose distinct metrics: {', '.join(METRIC_NAMES)}")
    manifest, planned = planned_answers(output)
    ensure_manifest(
        output / "judge.json",
        {
            "run_fingerprint": manifest["fingerprint"],
            "judge": judge,
            "ragas_version": version("ragas"),
            "metrics": list(METRIC_NAMES),
            "implementation": 1,
        },
    )
    latest = latest_records(output / "scores.jsonl", ("id", "mode", "metric"))
    work = []
    for row in planned:
        for name in names:
            old = latest.get((row["id"], row["mode"], name), {})
            if old.get("answer_fingerprint") == fingerprint(row) and old.get("status") in (
                "ok",
                "skipped",
            ):
                continue
            work.append((row, name))
    _notify(progress, "start", total=len(planned) * len(names), remaining=len(work))
    adapter = None
    try:
        needed = {name for row, name in work if not skip_reason(row, name)}
        if needed and metrics is None:
            _notify(progress, "item", label="initializing Ragas judge")
            adapter = RagasJudge(judge)
            metrics = adapter.metrics(needed)
        for row, name in work:
            label = f"{row['id']} / {row['mode']} / {name}"
            _notify(progress, "item", label=label)
            result = await evaluate_one(row, name, (metrics or {}).get(name))
            append_jsonl(
                output / "scores.jsonl",
                {
                    "id": row["id"],
                    "mode": row["mode"],
                    "metric": name,
                    "answer_fingerprint": fingerprint(row),
                    **result,
                },
            )
            _notify(
                progress,
                "done",
                status=result["status"],
                label=label,
                detail=result.get("reason", result.get("error", "")),
            )
            if result.get("reason") == "authentication_error":
                break
    finally:
        if adapter:
            await adapter.close()
    from .report import report

    summary = report(output)
    return sum(s["error"] + s["undefined"] + s["missing"] for s in summary if s["metric"] in names)


# Public convenience entrypoint, imported lazily to keep report's dependency one-way.
def report(output: Path) -> list[dict]:
    from .report import report as build_report

    return build_report(output)
