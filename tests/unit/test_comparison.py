import csv

from mnogobase.core.config import Settings
from mnogobase.core.files import append_jsonl, fingerprint
from mnogobase.evaluation.artifacts import ensure_manifest
from mnogobase.evaluation.core import prepare_run, report


def test_paired_comparison_uses_same_questions_and_reports_categories(tmp_path):
    path = tmp_path / "q.jsonl"
    for i, category in [(1, "fact"), (2, "multi_hop"), (3, "fact")]:
        append_jsonl(path, {"id": str(i), "question": f"Q{i}", "category": category})
    output = tmp_path / "run"
    run = prepare_run(path, output, Settings(), modes=["rag", "wiki"])
    ensure_manifest(output / "judge.json", {"run_fingerprint": run["fingerprint"]})
    for mode in ["rag", "wiki"]:
        for i in range(1, 4):
            row = {
                "id": str(i),
                "question": f"Q{i}",
                "category": "multi_hop" if i == 2 else "fact",
                "mode": mode,
                "text": "A",
                "contexts": ["C"],
                "status": "ok",
            }
            append_jsonl(output / "answers.jsonl", row)
            if (mode, i) in [("rag", 3), ("wiki", 2)]:
                continue
            append_jsonl(
                output / "scores.jsonl",
                {
                    "id": str(i),
                    "mode": mode,
                    "metric": "faithfulness",
                    "status": "ok",
                    "value": 1 if mode == "rag" else 0.5,
                    "answer_fingerprint": fingerprint(row),
                },
            )
    report(output)
    assert (output / "paired.csv").exists()
    rows = list(csv.DictReader((output / "paired.csv").open()))
    faith = next(r for r in rows if r["metric"] == "faithfulness")
    assert faith["paired_n"] == "1"
    assert float(faith["delta_b_minus_a"]) == -0.5
    categories = list(csv.DictReader((output / "by_category.csv").open()))
    assert {r["category"] for r in categories} == {"fact", "multi_hop"}
    assert "## Парные сравнения" in (output / "report.md").read_text()


def test_processing_report_keeps_failed_attempts_and_labels_shared_cost(tmp_path):
    q = tmp_path / "q.jsonl"
    append_jsonl(q, {"id": "q", "question": "Q"})
    output = tmp_path / "run"
    prepare_run(q, output, Settings(), corpus={"project": "p", "fingerprint": "c"})
    for i, status in enumerate(["error", "ok"]):
        append_jsonl(
            output / "processing.jsonl",
            {
                "id": str(i),
                "phase": "build",
                "status": status,
                "scope": "shared preprocessing",
                "duration_ms": 100,
                "tokens_in": 20,
                "tokens_out": 3,
                "before": {"project": "p", "fingerprint": "b"},
                "after": {"project": "p", "fingerprint": "c"},
                "stages": [
                    {
                        "stage": "extract",
                        "duration_ms": 50,
                        "tokens_in": 20,
                        "tokens_out": 3,
                        "completed": 1,
                        "failed": i == 0,
                    }
                ],
            },
        )
    report(output)
    rows = list(csv.DictReader((output / "processing.csv").open()))
    assert len(rows) == 2 and {r["status"] for r in rows} == {"error", "ok"}
    text = (output / "report.md").read_text()
    assert "общая предобработка" in text
    assert "200" in text  # Failed work is included in total wall time.
