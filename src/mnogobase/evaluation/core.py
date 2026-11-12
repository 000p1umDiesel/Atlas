"""Генерация ответов и их оценка по неизменяемому плану вопрос × режим.

`prepare_run` пишет план в `run.json`; `planned_answers` сопоставляет план с последними
записями `answers.jsonl` и даёт ровно одну строку на каждую пару вопрос × режим (со статусом
`missing`, если ответа ещё нет); `run_answers` и `score_run` доделывают только то, что ещё
не готово, и дописывают результаты в JSONL.

Ход работы передаётся необязательным колбэком `progress(event, **fields)` с событиями
`start` (total, remaining), `item` (label) и `done` (status, label, detail); его рисует CLI.
"""

from __future__ import annotations

import json
from importlib.metadata import version
from pathlib import Path

from mnogobase.core.config import Settings
from mnogobase.core.files import append_jsonl, fingerprint
from mnogobase.core.models import Answer
from mnogobase.llm.templates import render_messages
from mnogobase.retrieval.compare import run_mode

from .artifacts import ensure_manifest, latest_records, load_questions
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
    """Создаёт или проверяет план эксперимента `output/run.json` и возвращает манифест.

    `modes` по умолчанию — rag, wiki и graph (all нужно выбрать явно); `limit` берёт
    первые N вопросов; `k` по умолчанию — `retrieval.k`. В план входят снимок настроек (без `evaluation` и путей логов и
    прогонов, которые не влияют на ответы), отрендеренный промпт ответа с подстановками
    вместо данных (смена текста промпта меняет план) и `corpus` — снимок состояния корпуса.
    Если план уже есть и отличается, `ValueError` (см. `ensure_manifest`)."""
    questions = load_questions(dataset)
    modes = list(("rag", "wiki", "graph") if modes is None else modes)
    if not modes or len(set(modes)) != len(modes) or set(modes) - set(MODES):
        raise ValueError("Выберите неповторяющиеся режимы из списка: rag, wiki, graph, all")
    if limit is not None and limit < 1:
        raise ValueError("limit должен быть положительным")
    if k is not None and k < 1:
        raise ValueError("k должен быть положительным")
    snapshot = settings.model_dump(mode="json", exclude={"evaluation", "logs_dir", "runs_dir"})
    config = {
        "implementation": 1,
        "dataset_fingerprint": fingerprint(questions),
        "questions": questions[:limit],
        "modes": modes,
        "settings": snapshot,
        "k": k if k is not None else settings.retrieval.k,
        "answer_prompt": render_messages(
            "answer", question="$question", context="$context", language="$language"
        ),
    }
    if corpus is not None:
        config["corpus"] = corpus
    return ensure_manifest(output / "run.json", config)


def load_run(output: Path) -> dict:
    """Читает манифест `run.json` и проверяет его отпечаток: вручную испорченный план —
    `ValueError`, нет файла — `FileNotFoundError`."""
    path = output / "run.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("fingerprint") != fingerprint(manifest["config"]):
        raise ValueError(f"{path}: отпечаток манифеста не совпадает с содержимым")
    return manifest


def planned_answers(output: Path):
    """Возвращает `(manifest, planned)`: план и по одной строке ответа на каждую пару
    вопрос × режим в порядке плана.

    Строка — последняя запись из `answers.jsonl` или заглушка со статусом `missing`.
    Проверяется, что записи соответствуют плану (тот же вопрос и эталон), а успешные ответы
    непусты и содержат полные тексты контекста (`contexts`), без которых часть метрик
    посчитать нельзя. Нарушение — `ValueError`: артефакты повреждены или относятся к другому
    набору."""
    manifest = load_run(output)
    latest = latest_records(output / "answers.jsonl", ("id", "mode"))
    planned = []
    for sample in manifest["config"]["questions"]:
        for mode in manifest["config"]["modes"]:
            row = latest.get((sample["id"], mode), {**sample, "mode": mode, "status": "missing"})
            if row.get("question") != sample["question"] or row.get("reference") != sample.get(
                "reference"
            ):
                raise ValueError(f"Ответ не соответствует набору вопросов: {sample['id']} / {mode}")
            if row["status"] == "ok":
                answer = Answer.model_validate(row)
                if not answer.text.strip():
                    raise ValueError(f"Пустой ответ: {sample['id']} / {mode}")
                if "contexts" not in row or not all(isinstance(c, str) for c in row["contexts"]):
                    raise ValueError(f"Нет полных текстов контекста: {sample['id']} / {mode}")
            planned.append(row)
    return manifest, planned


def _notify(progress, event: str, **fields):
    """Передаёт событие в колбэк прогресса, если он задан."""
    if progress is not None:
        progress(event, **fields)


async def run_answers(
    output: Path, retrievers: dict, answerer, translator=None, *, progress=None
) -> int:
    """Генерирует недостающие ответы плана и возвращает, сколько ответов не готово.

    Обрабатываются пары со статусом не `ok` (ошибка прошлой попытки или нет записи). Ответ
    строится так же, как в `ask`/`compare` (`run_mode`), с `k` из плана. Результат каждой
    пары сразу дописывается в `answers.jsonl`, так что прерванный прогон теряет не больше
    одной пары. Любая ошибка, включая пустой ответ, записывается как статус `error` только с
    именем типа исключения: текст ошибок провайдера может содержать секреты. Делает сетевые
    вызовы retriever'ов и LLM."""
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
                raise ValueError("Модель вернула пустой ответ")
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
    """Оценивает ответы плана метриками `names` (по умолчанию все пять) и возвращает число
    пар ответ × метрика, оставшихся без оценки (ошибка, undefined или нет записи).

    `judge` — настройки судьи из `ragas.judge_config`; вместе с отпечатком плана и версией
    Ragas они фиксируются в `judge.json`, так что сменить судью в том же каталоге нельзя.
    Пара пропускается, если для того же ответа (тот же `answer_fingerprint`) уже есть оценка
    со статусом `ok` или `skipped`; ошибки и undefined оцениваются заново. Судья Ragas
    создаётся, только если есть что реально оценивать (`metrics` позволяет тестам передать
    готовые метрики). Каждая оценка сразу дописывается в `scores.jsonl`. После первой ошибки
    аутентификации цикл прерывается: остальные вызовы с тем же ключом тоже упали бы. В конце
    пересобираются отчёты."""
    names = list(METRIC_NAMES if names is None else names)
    if not names or len(set(names)) != len(names) or set(names) - set(METRIC_NAMES):
        raise ValueError(f"Выберите неповторяющиеся метрики из списка: {', '.join(METRIC_NAMES)}")
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
            _notify(progress, "item", label="инициализация судьи Ragas")
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
    from .report import report  # report импортирует core: импорт внутри разрывает цикл

    summary = report(output)
    return sum(s["error"] + s["undefined"] + s["missing"] for s in summary if s["metric"] in names)


# Публичная точка входа для CLI; модуль report импортируется лениво, чтобы зависимость
# report → core оставалась односторонней.
def report(output: Path) -> list[dict]:
    """Пересобирает все отчёты каталога эксперимента и возвращает сводку (см.
    `report.report`)."""
    from .report import report as build_report

    return build_report(output)
