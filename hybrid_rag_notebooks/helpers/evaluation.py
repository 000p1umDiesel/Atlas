"""Проверка сохранённых ответов, возобновляемая оценка Ragas и отчёт по всем вопросам."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import pandas as pd
from tqdm.std import tqdm

from .artifacts import append_jsonl, ensure_manifest, fingerprint, latest_records
from .settings import judge_settings

REFERENCE_METRICS = {"context_precision_with_reference", "context_recall", "factual_correctness"}
CONTEXT_METRICS = {"faithfulness", "context_precision_with_reference", "context_recall"}
METRIC_NAMES = (
    "faithfulness",
    "answer_relevancy",
    "context_precision_with_reference",
    "context_recall",
    "factual_correctness",
)

METRIC_GUIDE = {
    "faithfulness": (
        "Опора на источники",
        "Подтверждается ли ответ контекстом?",
        "Сверьте каждое утверждение с источниками; затем проверьте промпт и генератор.",
    ),
    "answer_relevancy": (
        "Ответ по существу",
        "Отвечает ли текст на заданный вопрос?",
        "Проверьте уход от вопроса, лишний текст и embeddings судьи.",
    ),
    "context_precision_with_reference": (
        "Точность поиска",
        "Полезны ли первые найденные контексты относительно эталона?",
        "Посмотрите лишние фрагменты, порядок RRF и эффект reranker.",
    ),
    "context_recall": (
        "Полнота поиска",
        "Можно ли найти в контексте все факты эталона?",
        "Проверьте парсинг, размер чанков, перевод запроса и top-k.",
    ),
    "factual_correctness": (
        "Факты ответа",
        "Совпадают ли факты ответа с эталоном (F1)?",
        "Сравните полноту ответа с эталоном и проверьте сам эталон.",
    ),
}


def _authentication_error(exc):
    """Instructor может обернуть HTTP-ошибку; проверяем и исходное исключение."""
    pending, seen = [exc], set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        status = getattr(current, "status_code", None)
        if status is None:
            status = getattr(getattr(current, "response", None), "status_code", None)
        if status in (401, 403):
            return True
        pending.extend([current.__cause__, current.__context__])
        pending.extend(
            attempt.exception for attempt in getattr(current, "failed_attempts", []) or []
        )
    return False


async def evaluate_one(row, name, metric):
    """Проверяем применимость метрики; пропуск или сбой не подменяем нулевой оценкой."""
    contexts = row.get("retrieved_contexts", [])
    if row.get("status") != "ok":
        return {"status": "skipped", "reason": "inference_failed_or_missing", "value": None}
    if name in REFERENCE_METRICS and (not row.get("reference", "")):
        return {"status": "skipped", "reason": "no_reference", "value": None}
    if name in CONTEXT_METRICS and (not contexts):
        return {"status": "skipped", "reason": "empty_context", "value": None}
    try:
        if name == "faithfulness":
            result = await metric.ascore(
                user_input=row["question"], response=row["response"], retrieved_contexts=contexts
            )
        elif name == "answer_relevancy":
            result = await metric.ascore(user_input=row["question"], response=row["response"])
        elif name in ("context_precision_with_reference", "context_recall"):
            result = await metric.ascore(
                user_input=row["question"], reference=row["reference"], retrieved_contexts=contexts
            )
        elif name == "factual_correctness":
            result = await metric.ascore(response=row["response"], reference=row["reference"])
        else:
            raise ValueError(f"Unknown metric: {name}")
        value = float(result.value)
        if not math.isfinite(value):
            return {"status": "undefined", "reason": "nonfinite_metric", "value": None}
        return {"status": "ok", "value": value}
    except Exception as exc:
        result = {"status": "error", "error": f"{type(exc).__name__}: {exc}", "value": None}
        if _authentication_error(exc):
            result["reason"] = "authentication_error"
        return result


def score_summary(questions, answers, scores, metric_names):
    """Строим отчёт по полному плану; оценки изменившихся ответов считаем устаревшими."""
    records = []
    for sample in questions:
        row = answers.get(sample["id"], {**sample, "status": "missing"})
        for name in metric_names:
            score = scores.get((sample["id"], name), {})
            if score.get("answer_fingerprint") != fingerprint(row):
                score = {}
            records.append(
                {
                    "id": sample["id"],
                    "question": sample["question"],
                    "metric": name,
                    "status": score.get("status", "missing"),
                    "value": score.get("value"),
                    "detail": score.get("reason", score.get("error", "")),
                }
            )
    details = pd.DataFrame(records)
    summaries = []
    for name in metric_names:
        part = details[details.metric == name]
        ok = part[part.status == "ok"]
        summaries.append(
            {
                "metric": name,
                "mean": ok.value.mean() if len(ok) else None,
                "planned": len(part),
                **{
                    status: int((part.status == status).sum())
                    for status in ("ok", "skipped", "error", "undefined", "missing")
                },
            }
        )
    return (details, pd.DataFrame(summaries))


class EvaluationLab:
    def __init__(
        self, root, *, run_name="baseline_interactive_v1", eval_name="ragas_v1", judge=None
    ):
        self.run_dir = Path(root) / "outputs" / "runs" / run_name
        self.eval_dir = self.run_dir / "evaluations" / eval_name
        self.judge = dict(judge if judge is not None else judge_settings())
        self.questions = []
        self.answers = {}
        self.metrics = None
        self.clients = []

    def load(self):
        path = self.run_dir / "run.json"
        if not path.exists():
            raise FileNotFoundError(
                "Сначала сохраните пакет ответов в первом ноутбуке. Проверьте RUN_NAME."
            )
        self.manifest = json.loads(path.read_text(encoding="utf-8"))
        self.questions = self.manifest["config"]["questions"]
        self.answers = latest_records(self.run_dir / "answers.jsonl")
        for sample in self.questions:
            row = self.answers.get(sample["id"])
            if row and row.get("status") == "ok":
                if row.get("question") != sample["question"] or row.get("reference") != sample.get(
                    "reference"
                ):
                    raise ValueError(f"Answer does not match the dataset: {sample['id']}")
                if (
                    not isinstance(row.get("response"), str)
                    or not row["response"].strip()
                    or not isinstance(row.get("retrieved_contexts"), list)
                    or not all(isinstance(c, str) for c in row["retrieved_contexts"])
                ):
                    raise ValueError(f"Invalid answer/context: {sample['id']}")
                if row.get("context") != "\n\n".join(row["retrieved_contexts"]):
                    raise ValueError("Saved contexts differ from the prompt context")
        return pd.DataFrame(
            [
                {
                    "id": q["id"],
                    "вопрос": q["question"],
                    "эталон": bool(q.get("reference")),
                    "статус ответа": self.answers.get(q["id"], {}).get("status", "missing"),
                }
                for q in self.questions
            ]
        )

    def _make_metrics(self):
        from openai import AsyncOpenAI
        from ragas.embeddings import embedding_factory
        from ragas.llms import llm_factory
        from ragas.metrics.collections import (
            AnswerRelevancy,
            ContextPrecisionWithReference,
            ContextRecall,
            FactualCorrectness,
            Faithfulness,
        )

        judge = self.judge
        client = AsyncOpenAI(
            base_url=judge["base_url"],
            api_key=os.getenv("JUDGE_API_KEY") or os.getenv("LLM_API_KEY") or "local-no-key",
            timeout=judge["timeout"],
            max_retries=2,
        )
        # Эти embeddings использует только Answer Relevancy для сравнения вопросов по смыслу.
        embed = AsyncOpenAI(
            base_url=judge["embedding_url"],
            api_key=os.getenv("JUDGE_EMBED_API_KEY")
            or os.getenv("EMBED_API_KEY")
            or "local-no-key",
            timeout=judge["timeout"],
            max_retries=2,
        )
        self.clients.extend([client, embed])
        llm = llm_factory(judge["model"], client=client, temperature=judge["temperature"])
        embeddings = embedding_factory("openai", model=judge["embedding_model"], client=embed)
        return {
            "faithfulness": Faithfulness(llm=llm),
            "answer_relevancy": AnswerRelevancy(
                llm=llm, embeddings=embeddings, strictness=judge["strictness"]
            ),
            "context_precision_with_reference": ContextPrecisionWithReference(llm=llm),
            "context_recall": ContextRecall(llm=llm),
            "factual_correctness": FactualCorrectness(llm=llm, mode="f1"),
        }

    async def score(self, metric_names=None, limit=None):
        """Считаем только недостающие пары вопрос–метрика, сохраняя результат каждой пары."""
        import ragas

        names = list(METRIC_NAMES if metric_names is None else metric_names)
        if not names or set(names) - set(METRIC_NAMES):
            raise ValueError("Выберите хотя бы одну известную метрику.")
        if limit is not None and limit < 1:
            raise ValueError("Число вопросов должно быть положительным.")
        self.load()  # Перечитываем ответы: изменённая или неудачная попытка отменяет старую оценку.
        config = {
            "run_fingerprint": self.manifest["fingerprint"],
            "judge": self.judge,
            "ragas_version": ragas.__version__,
            "metrics": list(METRIC_NAMES),
        }
        ensure_manifest(self.eval_dir / "judge.json", config)
        path = self.eval_dir / "scores.jsonl"
        previous = latest_records(path, ("id", "metric"))
        work = []
        for sample in self.questions[:limit]:
            row = self.answers.get(sample["id"], {**sample, "status": "missing"})
            for name in names:
                old = previous.get((sample["id"], name), {})
                if old.get("answer_fingerprint") == fingerprint(row) and old.get("status") in (
                    "ok",
                    "skipped",
                ):
                    continue
                work.append((sample["id"], row, name))
        print(
            f"Весь план: {len(self.questions) * len(METRIC_NAMES)} пар; выбрано: {len(self.questions[:limit]) * len(names)}; осталось: {len(work)}",
            flush=True,
        )
        if work and self.metrics is None:
            print("Подготавливаем Ragas и подключения судьи…", flush=True)
            self.metrics = self._make_metrics()
        for sample_id, row, name in tqdm(work, desc="Оценка Ragas", unit="метрика"):
            result = await evaluate_one(row, name, self.metrics[name])
            append_jsonl(
                path,
                {"id": sample_id, "metric": name, "answer_fingerprint": fingerprint(row), **result},
            )
            if result["status"] in ("error", "undefined"):
                tqdm.write(f"{sample_id} / {name}: {result}")
            if result.get("reason") == "authentication_error":
                self.report()
                raise RuntimeError(
                    "API судьи или его embeddings вернул 401/403. Проверьте JUDGE_API_KEY "
                    "и JUDGE_EMBED_API_KEY, адреса и доступ к моделям. Непустой ключ судьи "
                    "имеет приоритет над ключом генератора. После исправления .env "
                    "перезапустите kernel и повторите расчёт; прогресс сохранён."
                )
        return self.report()

    def report(self):
        self.load()
        # Просмотр не вызывает судью и доступен до первого расчёта метрик.
        judge_path = self.eval_dir / "judge.json"
        if judge_path.exists():
            bound = json.loads(judge_path.read_text(encoding="utf-8"))["config"]
            if bound["run_fingerprint"] != self.manifest["fingerprint"]:
                raise ValueError("Эта оценка относится к другому запуску.")
        scores = latest_records(self.eval_dir / "scores.jsonl", ("id", "metric"))
        details, summary = score_summary(self.questions, self.answers, scores, METRIC_NAMES)
        self.eval_dir.mkdir(parents=True, exist_ok=True)
        details.to_csv(self.eval_dir / "details.csv", index=False)
        summary.to_csv(self.eval_dir / "summary.csv", index=False)
        remaining = int(summary[["error", "undefined", "missing"]].sum().sum())
        print(
            f"Успешных оценок: {summary.ok.sum()}; пропущено: {summary.skipped.sum()}; осталось/ошибок: {remaining}"
        )
        print(f"CSV и checkpoints: {self.eval_dir}")
        return details, summary

    async def close(self):
        for client in self.clients:
            await client.close()
        self.clients = []
        self.metrics = None
