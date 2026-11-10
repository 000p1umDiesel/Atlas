"""Печать отдельного ответа вместе с оценками, эталоном и контекстом."""

from __future__ import annotations


def print_example(sample_id, questions, answers, table, max_context=3000):
    question = next(q for q in questions if q["id"] == sample_id)
    answer = answers.get(sample_id, {})
    print(f"{sample_id}: {question['question']}")
    if question.get("source_pages"):
        print("Где искать в PDF: стр.", ", ".join(map(str, question["source_pages"])))
    part = table[table.id == sample_id][["metric", "status", "value"]]
    print(part.to_string(index=False))
    print("\nОтвет:\n" + answer.get("response", answer.get("error", "ответа нет")))
    if question.get("reference"):
        print("\nЭталон:\n" + question["reference"])
    else:
        print("\nОжидаемое поведение:\n" + question.get("expected_behavior", "—"))
    context = answer.get("context", "")
    print(f"\nКонтекст ({len(context)} символов):\n" + context[:max_context])
    if len(context) > max_context:
        print("…")
