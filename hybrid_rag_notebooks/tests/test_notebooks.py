"""Выполнение обычных ячеек на подставных API и проверки текстового вывода."""

import ast
import contextlib
import inspect
import io
import json
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from helpers.artifacts import latest_records  # noqa: E402
from helpers.evaluation import METRIC_NAMES, EvaluationLab, score_summary  # noqa: E402
from helpers.inspection import filter_report, print_example, print_table  # noqa: E402


class TextInspectionTests(unittest.TestCase):
    def test_table_prints_source_as_text(self):
        source = '<script>alert("x")</script>'
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            result = print_table(pd.DataFrame({"source": [source]}))
        self.assertIsNone(result)
        self.assertEqual(stream.getvalue().count(source), 1)

    def test_report_preserves_manual_evidence_for_missing_answers(self):
        questions = [
            {
                "id": "base",
                "question": "How many heads?",
                "reference": "8",
                "source_pages": [5],
                "source_sections": ["3.2.2"],
            },
            {"id": "seed", "question": "What seed?", "expected_behavior": "Seed is not given."},
        ]
        details, _ = score_summary(questions, {}, {}, METRIC_NAMES)
        selected = filter_report(
            details, questions, {}, metric="faithfulness", group="no_reference"
        )
        self.assertEqual(list(selected.id), ["seed"])
        selected = filter_report(details, questions, {}, metric="faithfulness", status="ok")
        self.assertTrue(selected.empty)
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            print_example("seed", details, {}, questions)
            print_example("base", details, {}, questions)
        text = stream.getvalue()
        for expected in (
            "Ожидаемый отказ",
            "Seed is not given.",
            "Страницы PDF: 5",
            "3.2.2",
            "Ответ не сохранён.",
        ):
            self.assertIn(expected, text)


class NotebookRunner:
    """Выполняем настоящие исходники ячеек с поддержкой top-level await."""

    def __init__(self, name):
        self.name = name
        self.notebook = json.loads((ROOT / name).read_text())
        self.ns = {"__name__": "__main__"}

    async def run(self, cell):
        compiled = compile(
            "".join(cell["source"]), self.name, "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
        )
        result = eval(compiled, self.ns)
        if inspect.isawaitable(result):
            await result

    async def tag(self, tag):
        cell = next(
            c for c in self.notebook["cells"] if tag in c.get("metadata", {}).get("tags", [])
        )
        await self.run(cell)


class NotebookWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_judge_cleanup_after_partial_initialization(self):
        runner = NotebookRunner("03_hybrid_rag_ragas.ipynb")
        await runner.tag("close")
        from unittest.mock import AsyncMock

        client = SimpleNamespace(close=AsyncMock())
        runner.ns["judge_client"] = client
        await runner.tag("close")
        client.close.assert_awaited_once()

    async def test_cells_search_generation_and_all_metrics_resume(self):
        import httpx
        import numpy as np
        from helpers.model_clients import ModelAPI
        from qdrant_client import QdrantClient

        calls, metric_calls, prompts = [], [], []

        def handler(request):
            calls.append(request.url.path)
            payload = json.loads(request.content) if request.content else {}
            if request.url.path.endswith("/models"):
                body = {"data": [{"id": "mock-model"}]}
            elif request.url.path.endswith("/tokenize"):
                self.assertEqual(request.url.path, "/tokenize")
                body = {"tokens": list(range(len(payload["content"].split())))}
            elif request.url.path.endswith("/embeddings"):
                body = {
                    "data": [
                        {"index": i, "embedding": [1.0, 0.0]}
                        for i, _ in enumerate(payload["input"])
                    ]
                }
            elif request.url.path.endswith("/rerank"):
                body = {
                    "results": [
                        {"index": i, "relevance_score": 0.8}
                        for i, _ in enumerate(payload["documents"])
                    ]
                }
            else:
                prompts.append(payload["messages"])
                content = (
                    "Backup policy"
                    if payload["messages"][0]["content"].startswith("Translate")
                    else "Копии хранят 30 дней. [1]"
                )
                body = {"choices": [{"message": {"content": content}}], "usage": {}}
            return httpx.Response(200, json=body)

        class Sparse:
            def __init__(self, *args):
                pass

            def query_embed(self, query):
                yield SimpleNamespace(indices=np.array([1]), values=np.array([1.0]))

            def passage_embed(self, texts):
                for text in texts:
                    yield from self.query_embed(text)

        class Metric:
            def __init__(self, name):
                self.name = name

            async def ascore(self, **kwargs):
                metric_calls.append((self.name, kwargs))
                return SimpleNamespace(value=0.8)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            documents = root / "data" / "documents"
            documents.mkdir(parents=True)
            (documents / "backups.md").write_text(
                "# Backups\n\nBackups are retained for 30 days. Restoration is tested monthly.\n",
                encoding="utf-8",
            )
            questions = [
                {
                    "id": "attn_08_head_dimensions" if i == 1 else f"q{i}",
                    "question": f"  How long are backups retained ({i})?  "
                    if i == 1
                    else f"How long are backups retained ({i})?",
                    "reference": "REFERENCE_SENTINEL Копии хранят 30 дней.",
                }
                for i in range(1, 8)
            ]
            questions.append(
                {
                    "id": "seed",
                    "question": "Какой random seed использовали?",
                    "expected_behavior": "Seed is not given.",
                }
            )
            questions_path = root / "data" / "questions.jsonl"
            questions_path.write_text(
                "".join(json.dumps(q, ensure_ascii=False) + "\n" for q in questions),
                encoding="utf-8",
            )
            qdrant = QdrantClient(":memory:")
            with (
                patch(
                    "helpers.model_clients.ModelAPI",
                    side_effect=lambda *a, **k: ModelAPI(
                        *a, **k, transport=httpx.MockTransport(handler)
                    ),
                ),
                patch("qdrant_client.QdrantClient", return_value=qdrant),
                patch("fastembed.SparseTextEmbedding", Sparse),
                patch("ragas.llms.llm_factory", return_value=object()),
                patch("ragas.embeddings.embedding_factory", return_value=object()),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
                warnings.catch_warnings(),
            ):
                warnings.filterwarnings("ignore", message="Payload indexes have no effect")
                runners = [
                    NotebookRunner(name)
                    for name in (
                        "01_hybrid_rag.ipynb",
                        "02_hybrid_rag_inference.ipynb",
                        "03_hybrid_rag_ragas.ipynb",
                    )
                ]
                index, rag, scoring = runners
                metric_patches = []
                metric_classes = (
                    "Faithfulness",
                    "AnswerRelevancy",
                    "ContextPrecisionWithReference",
                    "ContextRecall",
                    "FactualCorrectness",
                )
                for cls, name in zip(metric_classes, METRIC_NAMES, strict=True):
                    metric_patches.append(
                        patch(
                            "ragas.metrics.collections." + cls,
                            side_effect=lambda *a, _name=name, **k: Metric(_name),
                        )
                    )
                with contextlib.ExitStack() as stack:
                    for mock in metric_patches:
                        stack.enter_context(mock)
                    try:
                        for runner in runners:
                            before_notebook = len(calls)
                            for cell in runner.notebook["cells"]:
                                if cell["cell_type"] != "code":
                                    continue
                                tag = cell["metadata"]["tags"][0]
                                if tag == "close":
                                    continue
                                before = len(calls)
                                await runner.run(cell)
                                if tag == "bootstrap":
                                    runner.ns["ROOT"] = root
                                if tag == "settings" and "CFG" in runner.ns:
                                    for role in ("embed", "llm", "rerank", "vision"):
                                        runner.ns["CFG"][f"{role}_model"] = "mock-model"
                                if tag == "answer":
                                    self.assertEqual(calls[before:], ["/v1/chat/completions"])
                                    self.assertEqual(
                                        runner.ns["answer"]["context"],
                                        runner.ns["found"]["context"],
                                    )
                            if runner is scoring:
                                self.assertEqual(len(calls), before_notebook)
                        self.assertIsNot(index.ns, rag.ns)
                        self.assertEqual(rag.ns["INDEX_CONFIG"], index.ns["INDEX_CONFIG"])
                        self.assertTrue(index.ns["chunks"])
                        self.assertEqual(
                            index.ns["preview_chunk"]["text"], index.ns["chunks"][0]["text"]
                        )
                        self.assertEqual(
                            len(latest_records(rag.ns["RUN_DIR"] / "answers.jsonl")), 8
                        )
                        before = len(calls)
                        await index.tag("index")
                        await rag.tag("batch")
                        self.assertEqual(len(calls), before)
                        qdrant.delete_collection(index.ns["COLLECTION"])
                        await index.tag("collection")
                        await index.tag("index")
                        self.assertEqual(
                            qdrant.count(index.ns["COLLECTION"], exact=True).count,
                            len(index.ns["chunks"]),
                        )
                        # RRF explanation matches the actual Qdrant implementation.
                        table = rag.ns["fusion_table"]
                        self.assertTrue(((table.qdrant_rrf - table.manual_rrf).abs() < 1e-7).all())
                        self.assertTrue(prompts)
                        self.assertTrue(
                            all(
                                "REFERENCE_SENTINEL" not in json.dumps(messages)
                                for messages in prompts
                            )
                        )
                        self.assertEqual(scoring.ns["LIMIT"], 3)
                        self.assertEqual(scoring.ns["summary"].ok.sum(), 15)
                        self.assertEqual(scoring.ns["summary"].missing.sum(), 25)
                        self.assertEqual({name for name, _ in metric_calls}, set(METRIC_NAMES))
                        before_metrics = len(metric_calls)
                        scoring.ns["LIMIT"] = None
                        await scoring.tag("score")
                        self.assertEqual(scoring.ns["summary"].ok.sum(), 37)
                        self.assertEqual(scoring.ns["summary"].skipped.sum(), 3)
                        self.assertEqual(len(metric_calls) - before_metrics, 22)
                        await scoring.tag("score")
                        self.assertEqual(len(metric_calls) - before_metrics, 22)
                        self.assertTrue((scoring.ns["EVAL_DIR"] / "summary.csv").exists())
                        # Compare distinct saved runs, using only shared successful pairs.
                        import shutil

                        from helpers.artifacts import fingerprint, write_json

                        other_dir = root / "outputs" / "runs" / "other_run"
                        shutil.copytree(scoring.ns["RUN_DIR"], other_dir)
                        other_manifest = json.loads((other_dir / "run.json").read_text())
                        other_manifest["config"]["retrieval"]["mode"] = "dense"
                        other_manifest["fingerprint"] = fingerprint(other_manifest["config"])
                        write_json(other_dir / "run.json", other_manifest)
                        judge_path = (
                            other_dir / "evaluations" / scoring.ns["EVAL_NAME"] / "judge.json"
                        )
                        judge_manifest = json.loads(judge_path.read_text())
                        judge_manifest["config"]["run_fingerprint"] = other_manifest["fingerprint"]
                        judge_manifest["fingerprint"] = fingerprint(judge_manifest["config"])
                        write_json(judge_path, judge_manifest)
                        comparison_cell = next(
                            c
                            for c in scoring.notebook["cells"]
                            if "compare" in c.get("metadata", {}).get("tags", [])
                        )
                        comparison_cell = {
                            **comparison_cell,
                            "source": "".join(comparison_cell["source"]).replace(
                                'OTHER_RUN_NAME = ""', 'OTHER_RUN_NAME = "other_run"'
                            ),
                        }
                        await scoring.run(comparison_cell)
                        self.assertEqual(scoring.ns["comparison"].n.sum(), 37)
                        self.assertTrue((scoring.ns["comparison"].delta == 0).all())
                        judge_manifest["config"]["judge"]["model"] = "different-judge"
                        write_json(judge_path, judge_manifest)
                        with self.assertRaisesRegex(ValueError, "Судья"):
                            await scoring.run(comparison_cell)
                        # An updated failure must invalidate an older successful metric.
                        from helpers.artifacts import append_jsonl

                        sample = scoring.ns["questions"][0]
                        append_jsonl(
                            scoring.ns["RUN_DIR"] / "answers.jsonl", {**sample, "status": "error"}
                        )
                        await scoring.tag("score")
                        self.assertEqual(scoring.ns["summary"].skipped.sum(), 8)
                        # Generation receives question and context, never the reference.
                        self.assertTrue(
                            all(
                                "reference" not in kwargs
                                for name, kwargs in metric_calls
                                if name == "faithfulness"
                            )
                        )
                    finally:
                        await scoring.tag("close")
                        for name in ("embedder", "generator", "reranker"):
                            for runner in (index, rag):
                                client = runner.ns.get(name)
                                if client is not None:
                                    client.close()
                        qdrant.close()

    async def test_answer_validation_prevents_evaluating_a_different_context(self):
        from helpers.artifacts import append_jsonl, ensure_manifest

        with tempfile.TemporaryDirectory() as tmp:
            lab = EvaluationLab(tmp, run_name="run")
            sample = {"id": "q", "question": "How long?"}
            ensure_manifest(lab.run_dir / "run.json", {"questions": [sample]})
            append_jsonl(
                lab.run_dir / "answers.jsonl",
                {
                    **sample,
                    "status": "ok",
                    "response": "30 days",
                    "retrieved_contexts": ["30 days"],
                    "context": "a different prompt",
                },
            )
            with self.assertRaisesRegex(ValueError, "differ from the prompt"):
                lab.load()


class ExplicitCellTests(unittest.IsolatedAsyncioTestCase):
    async def test_generation_uses_current_prompt_and_never_searches(self):
        runner = NotebookRunner("02_hybrid_rag_inference.ipynb")
        calls = []

        def chat(messages, **kwargs):
            calls.append(messages)
            return {"choices": [{"message": {"content": "answer [1]"}}]}

        import time

        runner.ns.update(
            time=time,
            ANSWER_PROMPT="old {question} {context}",
            generator=SimpleNamespace(chat=chat),
        )
        await runner.tag("answer_function")
        runner.ns["ANSWER_PROMPT"] = "new {question} {context}"
        found = {"question": "Q", "context": "[1] evidence", "sources": [{}], "latency_ms": 0}
        result = runner.ns["generate_answer"](found)
        self.assertEqual(calls[0][1]["content"], "new Q [1] evidence")
        self.assertEqual(result["context"], found["context"])
        self.assertEqual(result["response"], "answer [1]")

    async def test_inline_metrics_skip_missing_inputs_and_preserve_errors(self):
        import math

        from helpers.evaluation import CONTEXT_METRICS, REFERENCE_METRICS, _authentication_error

        class Metric:
            async def ascore(self, **kwargs):
                return SimpleNamespace(value=float("nan"))

        runner = NotebookRunner("03_hybrid_rag_ragas.ipynb")
        runner.ns.update(
            math=math,
            CONTEXT_METRICS=CONTEXT_METRICS,
            REFERENCE_METRICS=REFERENCE_METRICS,
            _authentication_error=_authentication_error,
            metrics={"faithfulness": Metric()},
        )
        await runner.tag("metric_function")
        score = runner.ns["score_one"]
        row = {"status": "ok", "question": "Q", "response": "A", "retrieved_contexts": ["C"]}
        self.assertEqual((await score(row, "faithfulness"))["status"], "undefined")
        self.assertEqual((await score(row, "context_recall"))["reason"], "no_reference")
        self.assertEqual(
            (await score({**row, "retrieved_contexts": []}, "faithfulness"))["reason"],
            "empty_context",
        )
        self.assertEqual(
            (await score({**row, "status": "error"}, "faithfulness"))["reason"],
            "inference_failed_or_missing",
        )
        self.assertEqual((await score(row, "answer_relevancy"))["status"], "error")

    async def test_chunker_splits_long_text_and_preserves_sections(self):
        from docling_core.transforms.chunker.hybrid_chunker import HybridChunker
        from docling_core.types.doc import DocItemLabel, DoclingDocument
        from helpers.parsing import ServerTokenizer

        api = SimpleNamespace(
            http=SimpleNamespace(base_url="http://fake/v1"),
            request=lambda *a, **k: {"tokens": list(range(len(k["json"]["content"].split())))},
        )
        tokenizer = ServerTokenizer(api=api, max_tokens=64)
        doc = DoclingDocument(name="sections")
        doc.add_heading("Alpha", level=1)
        doc.add_text(label=DocItemLabel.TEXT, text=" ".join(["alpha fact."] * 150))
        doc.add_heading("Beta", level=1)
        doc.add_text(label=DocItemLabel.TEXT, text=" ".join(["beta fact."] * 150))
        runner = NotebookRunner("01_hybrid_rag.ipynb")
        await runner.tag("chunk_function")
        chunks = runner.ns["split_document"](doc, "sections.md", "demo", tokenizer)
        self.assertGreater(len(chunks), 2)
        self.assertTrue(all(c["n_tokens"] <= 64 for c in chunks))
        self.assertTrue(any("Alpha" in c["headings"] for c in chunks))
        self.assertTrue(any("Beta" in c["headings"] for c in chunks))
        self.assertTrue(all(not ("alpha" in c["text"] and "beta" in c["text"]) for c in chunks))
        raw = list(HybridChunker(tokenizer=tokenizer, merge_peers=True).chunk(doc))
        self.assertIn("Alpha", HybridChunker(tokenizer=tokenizer).contextualize(raw[0]))

    async def test_comparison_can_run_with_disabled_reranker(self):
        runner = NotebookRunner("02_hybrid_rag_inference.ipynb")

        def search(question, settings):
            if settings["rerank"]:
                raise RuntimeError("Reranker is disabled")
            return {"sources": [{"ref": "chunk"}]}

        runner.ns.update(
            pd=pd,
            tqdm=lambda items, **k: items,
            question="Q",
            reranker=None,
            SEARCH={"rerank": False},
            search_question=search,
            generate_answer=lambda found: {"response": "A", "latency_ms": 0},
        )
        with contextlib.redirect_stdout(io.StringIO()):
            await runner.tag("experiments")
        self.assertEqual(len(runner.ns["comparison"]), 3)

    async def test_partial_docling_conversion_is_not_cached(self):
        from docling.datamodel.base_models import ConversionStatus
        from docling_core.types.doc import DocItemLabel, DoclingDocument

        doc = DoclingDocument(name="partial")
        doc.add_text(label=DocItemLabel.TEXT, text="One page survived, another failed.")
        result = SimpleNamespace(
            document=doc, status=ConversionStatus.PARTIAL_SUCCESS, errors=["bad page"]
        )
        runner = NotebookRunner("01_hybrid_rag.ipynb")
        from helpers.artifacts import fingerprint, write_json

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner.ns.update(
                CFG={"ocr": False, "device": "cpu"},
                paths=[root / "bad.pdf"],
                inventory=[{"path": "bad.pdf"}],
                INDEX_DIR=root,
                write_json=write_json,
                fingerprint=fingerprint,
                tqdm=lambda items, **k: items,
            )
            with (
                patch(
                    "docling.document_converter.DocumentConverter",
                    return_value=SimpleNamespace(convert=lambda path: result),
                ),
                self.assertRaisesRegex(RuntimeError, "partial_success"),
            ):
                await runner.tag("parse")
            self.assertFalse((root / "docling").exists())

    async def test_basic_parser_rejects_unimplemented_vision_mode(self):
        runner = NotebookRunner("01_hybrid_rag.ipynb")
        from helpers.settings import rag_settings

        runner.ns.update(
            CFG={**rag_settings(), "vision": True},
            discover_documents=lambda path: [],
            document_inventory=lambda paths, root: [],
            DOCUMENTS_DIR=Path("unused"),
            INDEX_DIR=Path("unused"),
            dimension=2,
            COLLECTION="test",
            ensure_manifest=lambda *args: None,
            pd=pd,
        )
        with (
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(ValueError, "VLM"),
        ):
            await runner.tag("inventory")


if __name__ == "__main__":
    unittest.main()
