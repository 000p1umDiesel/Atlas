"""Сводные таблицы по оценкам Ragas."""

from __future__ import annotations

import pandas as pd

METRIC_NAMES = (
    "faithfulness",
    "answer_relevancy",
    "context_precision_with_reference",
    "context_recall",
    "factual_correctness",
)
# Без эталона эти метрики посчитать нельзя.
REFERENCE_METRICS = {"context_precision_with_reference", "context_recall", "factual_correctness"}
# Без найденного контекста эти метрики теряют смысл.
CONTEXT_METRICS = {"faithfulness", "context_precision_with_reference", "context_recall"}
SHORT_NAMES = {
    "faithfulness": "faith",
    "answer_relevancy": "relevancy",
    "context_precision_with_reference": "ctx_precision",
    "context_recall": "ctx_recall",
    "factual_correctness": "factual",
}


def is_auth_error(exc: BaseException) -> bool:
    """401/403 где-то в цепочке исключений (instructor и openai заворачивают ошибки)."""
    pending, seen = [exc], set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        status = getattr(current, "status_code", None) or getattr(
            getattr(current, "response", None), "status_code", None
        )
        if status in (401, 403):
            return True
        pending += [current.__cause__, current.__context__]
    return False


def score_table(questions, scores, run=None) -> pd.DataFrame:
    """Длинная таблица: строка на пару вопрос × метрика, включая ещё не посчитанные."""
    rows = []
    for q in questions:
        for metric in METRIC_NAMES:
            score = scores.get((q["id"], metric), {})
            rows.append(
                {
                    "run": run,
                    "id": q["id"],
                    "language": q.get("language"),
                    "answerable": bool(q.get("reference")),
                    "metric": metric,
                    "status": score.get("status", "missing"),
                    "value": score.get("value"),
                    "detail": score.get("reason") or score.get("error", ""),
                }
            )
    return pd.DataFrame(rows)


def status_counts(table: pd.DataFrame) -> pd.DataFrame:
    """Сколько оценок получено, пропущено по смыслу, упало или ещё не посчитано."""
    counts = table.pivot_table(index="metric", columns="status", values="id", aggfunc="count")
    return counts.reindex(METRIC_NAMES).fillna(0).astype(int)


def mean_scores(table: pd.DataFrame, by=("run",)) -> pd.DataFrame:
    """Средние только по успешным оценкам; пропуски нулями не считаются."""
    ok = table[table.status == "ok"].copy()
    ok["value"] = ok["value"].astype(float)
    result = ok.pivot_table(index=list(by), columns="metric", values="value", aggfunc="mean")
    result = result.reindex(columns=list(METRIC_NAMES)).rename(columns=SHORT_NAMES)
    return result.round(3)
