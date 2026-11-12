import csv
import json
from types import SimpleNamespace

import pytest

from mnogobase.core.config import Settings
from mnogobase.core.models import ContextItem
from mnogobase.retrieval.answer import Answerer
from tests.fakes import FakeLLM


def evaluation():
    from mnogobase.evaluation import core

    return core


def dataset(tmp_path, rows=None):
    path = tmp_path / "questions.jsonl"
    rows = (
        rows
        if rows is not None
        else [
            {"id": "q1", "question": "Why?", "reference": "Because.", "language": "en"},
            {"id": "q2", "question": "Почему?", "reference": "Причина."},
        ]
    )
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    return path


class Retriever:
    def retrieve(self, query, k, alt_queries=()):
        if query == "Почему?":
            raise RuntimeError("retrieval unavailable")
        return [ContextItem(kind="chunk", ref="c1", text="Because.")]


class RecordingRetriever(Retriever):
    def __init__(self):
        self.calls = []

    def retrieve(self, query, k, alt_queries=()):
        self.calls.append((query, k, alt_queries))
        return super().retrieve(query, k, alt_queries)


async def test_answer_preserves_only_full_blocks_sent_to_model():
    llm = FakeLLM(lambda task, prompt: "Answer [1]")
    text = "long evidence\n\nwith paragraphs " * 20
    answer = await Answerer(llm, 1).answer(
        "Q?",
        "rag",
        [
            ContextItem(kind="chunk", ref="c1", text=text),
            ContextItem(kind="chunk", ref="c2", text="excluded"),
        ],
    )
    assert answer.contexts == ["[1] (document c1)\n" + text]
    assert "\n\n".join(answer.contexts) in llm.calls_for("answer")[0]


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"id": "a", "question": " "}],
        [{"id": "a", "question": "q"}, {"id": "a", "question": "r"}],
        [{"id": "a", "question": "q", "reference": 4}],
    ],
)
def test_dataset_rejects_invalid_rows(tmp_path, rows):
    with pytest.raises(ValueError):
        evaluation().load_questions(dataset(tmp_path, rows))


async def test_run_three_modes_resume_and_report_incomplete_scores(tmp_path):
    core = evaluation()
    run = tmp_path / "run"
    manifest = core.prepare_run(dataset(tmp_path), run, Settings())
    assert manifest["config"]["modes"] == ["rag", "wiki", "graph"]
    llm = FakeLLM(lambda task, prompt: "Because [1].")
    retrievers = {m: Retriever() for m in ("rag", "wiki", "graph")}
    assert await core.run_answers(run, retrievers, Answerer(llm, 1000)) == 3
    first_calls = len(llm.calls)
    assert await core.run_answers(run, retrievers, Answerer(llm, 1000)) == 3
    assert len(llm.calls) == first_calls
    rows = core.latest_records(run / "answers.jsonl", ("id", "mode"))
    assert rows[("q1", "wiki")]["contexts"] == ["[1] (document c1)\nBecause."]
    assert rows[("q2", "graph")]["status"] == "error"
    summary = core.report(run)
    assert len(summary) == 15
    assert summary[0]["answers_ok"] == 1
    assert summary[0]["answers_error"] == 1
    assert summary[0]["missing"] == 2
    assert (run / "report.md").is_file()
    assert len(list(csv.DictReader((run / "details.csv").open()))) == 30


def test_manifest_blocks_changed_config_and_dataset(tmp_path):
    core = evaluation()
    path = dataset(tmp_path)
    run = tmp_path / "run"
    core.prepare_run(path, run, Settings(), limit=1)
    core.prepare_run(path, run, Settings(), limit=1)
    with pytest.raises(ValueError, match="configuration changed"):
        core.prepare_run(path, run, Settings(), limit=2)
    with pytest.raises(ValueError, match="configuration changed"):
        core.prepare_run(path, run, Settings(), limit=1, k=19)


def test_manifest_blocks_resume_after_security_policy_changes(tmp_path, monkeypatch):
    from mnogobase.llm import templates

    core = evaluation()
    path = dataset(tmp_path)
    run = tmp_path / "run"
    core.prepare_run(path, run, Settings())
    original = templates.render

    def changed_policy(name, **values):
        result = original(name, **values)
        return result + "\nUpdated security policy." if name == "untrusted_data" else result

    monkeypatch.setattr(templates, "render", changed_policy)
    with pytest.raises(ValueError, match="configuration changed"):
        core.prepare_run(path, run, Settings())


@pytest.mark.parametrize("existing_run", [False, True], ids=["new-run", "existing-run"])
@pytest.mark.parametrize(
    "options, match",
    [
        pytest.param({"modes": []}, "distinct modes", id="empty-modes"),
        pytest.param({"modes": ["rag", "rag"]}, "distinct modes", id="duplicate-modes"),
        pytest.param({"modes": ["rag", "unknown"]}, "distinct modes", id="unknown-mode"),
        pytest.param({"limit": 0}, "limit must be positive", id="zero-limit"),
        pytest.param({"limit": -1}, "limit must be positive", id="negative-limit"),
        pytest.param({"k": 0}, "k must be positive", id="zero-k"),
        pytest.param({"k": -1}, "k must be positive", id="negative-k"),
    ],
)
def test_invalid_run_options_do_not_create_or_replace_manifest(
    tmp_path, existing_run, options, match
):
    core = evaluation()
    questions = dataset(tmp_path)
    run = tmp_path / "run"
    manifest_path = run / "run.json"
    if existing_run:
        core.prepare_run(questions, run, Settings())
    before = manifest_path.read_bytes() if existing_run else None

    with pytest.raises(ValueError, match=match):
        core.prepare_run(questions, run, Settings(), **options)

    if existing_run:
        assert manifest_path.read_bytes() == before
        assert {path.name for path in run.iterdir()} == {"run.json"}
    else:
        assert not run.exists()


def test_run_accepts_positive_option_boundaries_and_distinct_all_mode(tmp_path):
    core = evaluation()
    run = tmp_path / "run"
    core.prepare_run(dataset(tmp_path), run, Settings(), modes=["rag", "all"], limit=1, k=1)

    config = core.load_run(run)["config"]
    assert config["questions"] == [
        {"id": "q1", "question": "Why?", "reference": "Because.", "language": "en"}
    ]
    assert config["modes"] == ["rag", "all"]
    assert config["k"] == 1


@pytest.mark.parametrize("tampered", ["fingerprint", "config"], ids=["digest", "config-body"])
async def test_tampered_manifest_blocks_resume_before_retrieval_and_preserves_files(
    tmp_path, tampered
):
    core = evaluation()
    run = tmp_path / "run"
    core.prepare_run(dataset(tmp_path), run, Settings(), modes=["rag"])
    manifest_path = run / "run.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if tampered == "fingerprint":
        manifest["fingerprint"] = "0" * 64
    else:
        manifest["config"]["questions"][0]["question"] = "Altered question?"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    before = manifest_path.read_bytes()
    retriever = RecordingRetriever()
    llm = FakeLLM(lambda *a: "Answer")

    with pytest.raises(ValueError, match="invalid manifest fingerprint"):
        core.load_run(run)
    with pytest.raises(ValueError, match="invalid manifest fingerprint"):
        await core.run_answers(run, {"rag": retriever}, Answerer(llm, 1000))

    assert retriever.calls == []
    assert llm.calls == []
    assert manifest_path.read_bytes() == before
    assert {path.name for path in run.iterdir()} == {"run.json"}


@pytest.mark.parametrize(
    "changes, omitted, match",
    [
        pytest.param(
            {"question": "Another question?"},
            None,
            "Answer does not match dataset: q1 / rag",
            id="mismatched-question",
        ),
        pytest.param(
            {"reference": "Another reference."},
            None,
            "Answer does not match dataset: q1 / rag",
            id="mismatched-reference",
        ),
        pytest.param(
            {}, "reference", "Answer does not match dataset: q1 / rag", id="missing-reference"
        ),
        pytest.param({"text": ""}, None, "Empty answer: q1 / rag", id="empty-answer"),
        pytest.param({"text": " \n\t "}, None, "Empty answer: q1 / rag", id="whitespace-answer"),
        pytest.param({}, "contexts", "Missing full contexts: q1 / rag", id="missing-contexts"),
        pytest.param({"contexts": [7]}, None, "contexts", id="integer-context"),
        pytest.param({"contexts": ["Evidence.", None]}, None, "contexts", id="mixed-context-types"),
        pytest.param({"contexts": "Evidence."}, None, "contexts", id="string-context-container"),
        pytest.param({"contexts": None}, None, "contexts", id="null-contexts"),
    ],
)
async def test_invalid_saved_answer_blocks_pending_retry_and_preserves_artifacts(
    tmp_path, changes, omitted, match
):
    from mnogobase.core.files import append_jsonl

    core = evaluation()
    run = tmp_path / "run"
    core.prepare_run(dataset(tmp_path), run, Settings(), modes=["rag"])
    assert (
        await core.run_answers(
            run, {"rag": Retriever()}, Answerer(FakeLLM(lambda *a: "Because [1]."), 1000)
        )
        == 1
    )
    answers_path = run / "answers.jsonl"
    rows = core.latest_records(answers_path, ("id", "mode"))
    assert rows[("q2", "rag")]["status"] == "error"
    invalid = {**rows[("q1", "rag")], **changes}
    if omitted is not None:
        invalid.pop(omitted)
    append_jsonl(answers_path, invalid)
    before = {path.name: path.read_bytes() for path in run.iterdir()}
    retriever = RecordingRetriever()
    llm = FakeLLM(lambda *a: "Answer")

    with pytest.raises(ValueError, match=match):
        core.planned_answers(run)
    with pytest.raises(ValueError, match=match):
        await core.run_answers(run, {"rag": retriever}, Answerer(llm, 1000))

    assert retriever.calls == []
    assert llm.calls == []
    assert {path.name: path.read_bytes() for path in run.iterdir()} == before


def test_jsonl_recovers_utf8_tail_but_rejects_middle_corruption(tmp_path):
    from mnogobase.core.files import append_jsonl, read_jsonl

    path = tmp_path / "a.jsonl"
    path.write_bytes(b'{"id":"ok"}\n{"id":"\xd0')
    assert read_jsonl(path, recover_tail=True) == [{"id": "ok"}]
    append_jsonl(path, {"id": "next"})
    assert read_jsonl(path) == [{"id": "ok"}, {"id": "next"}]
    path.write_bytes(b'broken\n{"id":"ok"}\n')
    with pytest.raises(ValueError, match="line 1"):
        read_jsonl(path, recover_tail=True)


class Metric:
    def __init__(self, value=0.75):
        self.value = value
        self.calls = []

    async def ascore(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.value, Exception):
            raise self.value
        return SimpleNamespace(value=self.value)


async def test_metric_arguments_and_skips():
    from mnogobase.evaluation.ragas import evaluate_one

    row = {"question": "Q", "text": "A", "reference": "R", "contexts": ["C"], "status": "ok"}
    expected = {
        "faithfulness": {"user_input": "Q", "response": "A", "retrieved_contexts": ["C"]},
        "answer_relevancy": {"user_input": "Q", "response": "A"},
        "context_precision_with_reference": {
            "user_input": "Q",
            "reference": "R",
            "retrieved_contexts": ["C"],
        },
        "context_recall": {"user_input": "Q", "reference": "R", "retrieved_contexts": ["C"]},
        "factual_correctness": {"response": "A", "reference": "R"},
    }
    for name, kwargs in expected.items():
        metric = Metric()
        assert await evaluate_one(row, name, metric) == {"status": "ok", "value": 0.75}
        assert metric.calls == [kwargs]
    metric = Metric()
    for changed, name, reason in [
        ({"contexts": []}, "faithfulness", "empty_context"),
        ({"reference": None}, "context_recall", "no_reference"),
        ({"status": "error"}, "answer_relevancy", "inference_failed_or_missing"),
    ]:
        result = await evaluate_one({**row, **changed}, name, metric)
        assert result["status"] == "skipped" and result["reason"] == reason
    assert metric.calls == []
    assert (await evaluate_one(row, "faithfulness", Metric(float("nan"))))["status"] == "undefined"
    assert (await evaluate_one(row, "faithfulness", Metric(RuntimeError("failed"))))[
        "status"
    ] == "error"


async def test_scores_resume_latest_failure_and_invalidate_changed_answers(tmp_path):
    core = evaluation()
    from mnogobase.core.files import append_jsonl

    run = tmp_path / "run"
    core.prepare_run(dataset(tmp_path), run, Settings(), modes=["rag"], limit=1)
    await core.run_answers(run, {"rag": Retriever()}, Answerer(FakeLLM(lambda *a: "Answer"), 1000))
    metrics = {name: Metric() for name in core.METRIC_NAMES}
    assert await core.score_run(run, {"model": "fake"}, metrics=metrics) == 0
    assert core.report(run)[0]["mean"] == 0.75
    assert await core.score_run(run, {"model": "fake"}, metrics=metrics) == 0
    assert len(metrics["faithfulness"].calls) == 1
    score = core.latest_records(run / "scores.jsonl", ("id", "mode", "metric"))[
        ("q1", "rag", "faithfulness")
    ]
    append_jsonl(run / "scores.jsonl", {**score, "status": "error", "value": None})
    assert core.report(run)[0]["error"] == 1
    await core.score_run(run, {"model": "fake"}, metrics=metrics)
    assert len(metrics["faithfulness"].calls) == 2
    answer = core.latest_records(run / "answers.jsonl", ("id", "mode"))[("q1", "rag")]
    append_jsonl(run / "answers.jsonl", {**answer, "text": "Changed"})
    assert core.report(run)[0]["missing"] == 1
    await core.score_run(run, {"model": "fake"}, metrics=metrics)
    assert len(metrics["faithfulness"].calls) == 3
    with pytest.raises(ValueError, match="configuration changed"):
        await core.score_run(run, {"model": "other"}, metrics=metrics)


def test_judge_inherits_connections_and_explicit_key_does_not_fall_back(monkeypatch):
    from mnogobase.evaluation.ragas import api_key, judge_config

    settings = Settings()
    conf = judge_config(settings)
    assert conf["base_url"] == settings.llm.url
    assert conf["model"] == settings.llm.model_for("answer")
    assert conf["embedding_url"] == settings.embedder.url
    monkeypatch.setenv("LLM_API_KEY", "secret")
    assert api_key(conf["api_key_env"]) == "secret"
    with pytest.raises(ValueError, match="MISSING_JUDGE_KEY"):
        api_key("MISSING_JUDGE_KEY")


async def test_failed_answer_retries_without_regenerating_successes(tmp_path):
    core = evaluation()
    run = tmp_path / "run"
    core.prepare_run(dataset(tmp_path), run, Settings(), modes=["rag"])
    llm = FakeLLM(lambda *a: "Answer")
    assert await core.run_answers(run, {"rag": Retriever()}, Answerer(llm, 1000)) == 1

    class RecoveredRetriever(Retriever):
        def retrieve(self, query, k, alt_queries=()):
            return super().retrieve("Why?", k, alt_queries)

    assert await core.run_answers(run, {"rag": RecoveredRetriever()}, Answerer(llm, 1000)) == 0
    assert len(llm.calls) == 2
    assert core.report(run)[0]["answers_ok"] == 2


async def test_metric_failure_saved_without_secrets_then_retried(tmp_path):
    core = evaluation()
    run = tmp_path / "run"
    core.prepare_run(dataset(tmp_path), run, Settings(), modes=["rag"], limit=1)
    await core.run_answers(run, {"rag": Retriever()}, Answerer(FakeLLM(lambda *a: "A"), 1000))
    metric = Metric(RuntimeError("request with secret-key-value"))
    assert (
        await core.score_run(
            run, {"model": "fake"}, names=["faithfulness"], metrics={"faithfulness": metric}
        )
        == 1
    )
    assert "secret-key-value" not in (run / "scores.jsonl").read_text()
    assert core.report(run)[0]["mean"] is None
    metric.value = 0.5
    assert (
        await core.score_run(
            run, {"model": "fake"}, names=["faithfulness"], metrics={"faithfulness": metric}
        )
        == 0
    )
    assert core.report(run)[0]["mean"] == 0.5
    assert core.report(run)[1]["missing"] == 1


async def test_authentication_failure_stops_after_checkpoint(tmp_path):
    core = evaluation()
    run = tmp_path / "run"
    core.prepare_run(dataset(tmp_path), run, Settings(), modes=["rag"], limit=1)
    await core.run_answers(run, {"rag": Retriever()}, Answerer(FakeLLM(lambda *a: "A"), 1000))

    class Unauthorized(Exception):
        status_code = 401

    metrics = {name: Metric(Unauthorized()) for name in core.METRIC_NAMES}
    assert await core.score_run(run, {"model": "fake"}, metrics=metrics) == 5
    saved = core.latest_records(run / "scores.jsonl", ("id", "mode", "metric"))
    assert len(saved) == 1
    assert next(iter(saved.values()))["reason"] == "authentication_error"
    assert sum(len(m.calls) for m in metrics.values()) == 1


async def test_no_context_or_reference_skips_only_inapplicable_metrics(tmp_path):
    core = evaluation()
    run = tmp_path / "run"
    questions = dataset(tmp_path, [{"id": "q", "question": "Unknown?"}])
    core.prepare_run(questions, run, Settings(), modes=["graph"])

    class EmptyRetriever:
        def retrieve(self, *a):
            return []

    await core.run_answers(
        run,
        {"graph": EmptyRetriever()},
        Answerer(FakeLLM(lambda *a: pytest.fail("generation on empty context")), 1000),
    )
    metrics = {name: Metric(0.0) for name in core.METRIC_NAMES}
    assert await core.score_run(run, {"model": "fake"}, metrics=metrics) == 0
    summary = {s["metric"]: s for s in core.report(run)}
    assert summary["answer_relevancy"]["mean"] == 0.0
    assert summary["faithfulness"]["skipped"] == 1
    assert summary["factual_correctness"]["skipped"] == 1
    assert sum(len(m.calls) for m in metrics.values()) == 1


async def test_ragas_adapter_constructs_all_metrics_without_network(monkeypatch):
    from mnogobase.evaluation.ragas import METRIC_NAMES, RagasJudge, judge_config

    monkeypatch.delenv("LLM_API_KEY", raising=False)
    config = judge_config(Settings())
    config["api_key_env"] = None
    config["embedding_api_key_env"] = None
    judge = RagasJudge(config)
    try:
        metrics = judge.metrics(METRIC_NAMES)
        assert metrics["factual_correctness"].mode == "f1"
        assert set(metrics) == set(METRIC_NAMES)
    finally:
        await judge.close()


async def test_empty_model_answer_remains_retryable(tmp_path):
    core = evaluation()
    run = tmp_path / "run"
    core.prepare_run(dataset(tmp_path), run, Settings(), modes=["rag"], limit=1)
    assert (
        await core.run_answers(run, {"rag": Retriever()}, Answerer(FakeLLM(lambda *a: "  "), 1000))
        == 1
    )
    assert core.report(run)[0]["answers_error"] == 1
    assert (
        await core.run_answers(
            run, {"rag": Retriever()}, Answerer(FakeLLM(lambda *a: "Answer"), 1000)
        )
        == 0
    )
    assert core.report(run)[0]["answers_ok"] == 1
