"""Текстовый просмотр таблиц, чанков и сохранённых ответов в обычных ячейках."""

from __future__ import annotations

import pandas as pd

from .retrieval import chunk_text


def print_table(frame):
    """Печатаем таблицу через stdout, без rich display и HTML pandas."""
    print(frame.to_string(index=False, max_colwidth=100) if not frame.empty else "Строк нет.")


def print_chunk(chunk):
    print(f"Чанк: {chunk['chunk_id']}")
    print(f"Файл: {chunk['path']} · страница: {chunk.get('page', '—')}")
    print(f"Токенов: {chunk.get('n_tokens', '—')}")
    print(chunk_text(chunk))


def ranking_table(result):
    return pd.DataFrame(
        [
            {
                "до reranker": hit["rrf_rank"],
                "после": i + 1,
                "поднялся на": hit["rrf_rank"] - i - 1,
                "RRF": hit["rrf_score"],
                "reranker": hit.get("rerank_score"),
                "файл": hit["payload"].get("path"),
                "чанк": hit["payload"]["chunk_id"],
                "текст": chunk_text(hit["payload"])[:180],
            }
            for i, hit in enumerate(result["candidates"])
        ]
    )


def filter_report(details, questions, answers, *, metric, status="all", group="all"):
    """Отбираем примеры: метрика, статус и наличие эталона задаются в ячейке."""
    if metric not in set(details.metric):
        raise ValueError(f"Неизвестная метрика: {metric}")
    if status not in {"all", "problems", "ok"}:
        raise ValueError("status: all, problems или ok")
    if group not in {"all", "reference", "no_reference"}:
        raise ValueError("group: all, reference или no_reference")
    samples = {**answers, **{q["id"]: q for q in questions}}
    part = details[details.metric == metric].copy()
    if group != "all":
        has_reference = part.id.map(lambda key: bool(samples.get(key, {}).get("reference")))
        part = part[has_reference if group == "reference" else ~has_reference]
    if status == "problems":
        part = part[part.status != "ok"]
    elif status == "ok":
        part = part[part.status == "ok"]
    return part.sort_values("value", na_position="last").reset_index(drop=True)


def print_example(sample_id, details, answers, questions):
    """Печатаем ответ, критерий проверки и точный контекст, в том числе для пропусков."""
    samples = {**answers, **{q["id"]: q for q in questions}}
    if sample_id not in samples:
        raise ValueError(f"Неизвестный id вопроса: {sample_id}")
    sample = samples[sample_id]
    row = answers.get(sample_id, {})
    print(f"{sample_id}: {sample['question']}")
    if sample.get("source_pages"):
        print("Страницы PDF:", ", ".join(map(str, sample["source_pages"])))
    if sample.get("source_sections"):
        print("Разделы:", ", ".join(sample["source_sections"]))
    print_table(details[details.id == sample_id][["metric", "status", "value", "detail"]])
    print("\nОтвет RAG:\n" + row.get("response", row.get("error", "Ответ не сохранён.")))
    reference = sample.get("reference") or row.get("reference")
    if reference:
        print("\nЭталон:\n" + reference)
    else:
        print("\nОжидаемый отказ:\n" + sample.get("expected_behavior", "Критерий не задан."))
    print("\nТочный контекст:\n" + row.get("context", "Контекст не сохранён."))
