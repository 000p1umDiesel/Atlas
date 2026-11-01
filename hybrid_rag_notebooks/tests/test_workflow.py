"""Проверки без скачивания моделей и вызова реальных API."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from helpers.evaluation import CONTEXT_METRICS, evaluate_one, score_summary  # noqa: E402
from helpers.retrieval import (  # noqa: E402
    HybridRetriever,
    build_context,
    point_id,
    prepare_collection,
)


class DeliveryTests(unittest.TestCase):
    def test_notebooks_have_visible_pipeline_and_no_project_imports(self):
        for name in (
            "01_hybrid_rag.ipynb",
            "02_hybrid_rag_inference.ipynb",
            "03_hybrid_rag_ragas.ipynb",
        ):
            path = ROOT / name
            self.assertTrue(path.exists(), name)
            notebook = json.loads(path.read_text())
            code = "\n".join(
                "".join(c["source"]) for c in notebook["cells"] if c["cell_type"] == "code"
            )
            self.assertNotIn("from mnogobase", code)
            for rich_output in ("helpers.widgets", "ipywidgets", "RAGLab", "EvaluationLab"):
                self.assertNotIn(rich_output, code)
            for cell in notebook["cells"]:
                if cell["cell_type"] == "code":
                    self.assertEqual(cell["outputs"], [])
                    compile("".join(cell["source"]), name, "exec", flags=0x2000)


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        from helpers import artifacts

        self.a = artifacts

    def test_resume_uses_latest_status_and_rejects_changed_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.a.ensure_manifest(root / "run.json", {"model": "one"})
            self.a.append_jsonl(root / "answers.jsonl", {"id": "q1", "status": "ok"})
            self.a.append_jsonl(root / "answers.jsonl", {"id": "q1", "status": "error"})
            self.assertEqual(self.a.latest_records(root / "answers.jsonl")["q1"]["status"], "error")
            with self.assertRaises(ValueError):
                self.a.ensure_manifest(root / "run.json", {"model": "two"})

    def test_invalid_dataset_is_rejected_with_line_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "q.jsonl"
            path.write_text('{"id":"a","question":"ok"}\n{"id":"a","question":"again"}\n')
            with self.assertRaisesRegex(ValueError, "2"):
                self.a.load_questions(path)

    def test_question_whitespace_is_normalized_before_saving_a_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "q.jsonl"
            path.write_text(json.dumps({"id": "q1", "question": "  What is attention?\n"}) + "\n")
            self.assertEqual(self.a.load_questions(path)[0]["question"], "What is attention?")


class ClientTests(unittest.TestCase):
    def test_embeddings_sorted_normalized_and_dimension_validated(self):
        import httpx
        from helpers.model_clients import ModelAPI

        def handler(request):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 1, "embedding": [0.0, 4.0]},
                        {"index": 0, "embedding": [3.0, 0.0]},
                    ]
                },
            )

        with ModelAPI("http://test/v1", "embed", transport=httpx.MockTransport(handler)) as api:
            self.assertEqual(api.embed(["a", "b"]), [[1.0, 0.0], [0.0, 1.0]])
            with self.assertRaises(ValueError):
                api.embed(["a", "b"], dim=3)

    def test_rerank_restores_input_order(self):
        import httpx
        from helpers.model_clients import ModelAPI

        def handler(request):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"index": 1, "relevance_score": 0.9},
                        {"index": 0, "relevance_score": 0.1},
                    ]
                },
            )

        with ModelAPI("http://test/v1", "rank", transport=httpx.MockTransport(handler)) as api:
            self.assertEqual(api.rerank("q", ["a", "b"]), [0.1, 0.9])


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace

        import httpx
        import numpy as np
        from helpers.model_clients import ModelAPI
        from qdrant_client import QdrantClient
        from qdrant_client import models as qm

        self.client = QdrantClient(":memory:")
        self.api_calls = []

        def handler(request):
            self.api_calls.append(request.url.path)
            payload = json.loads(request.content)
            if request.url.path.endswith("/embeddings"):
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {"index": i, "embedding": [1.0, 0.0]}
                            for i, _ in enumerate(payload["input"])
                        ]
                    },
                )
            if request.url.path.endswith("/rerank"):
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "index": i,
                                "relevance_score": 1.0
                                if "30 days" in text
                                else (0.9 if "monthly" in text else 0.1),
                            }
                            for i, text in enumerate(payload["documents"])
                        ]
                    },
                )
            prompt = payload["messages"][0]["content"]
            content = (
                "How long are backups retained?"
                if prompt.startswith("Translate")
                else "Копии хранятся 30 дней. [1]"
            )
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": content}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                },
            )

        self.api = ModelAPI("http://test/v1", "fake", transport=httpx.MockTransport(handler))
        sparse = SimpleNamespace(indices=np.array([1]), values=np.array([1.0]))
        bm25 = SimpleNamespace(query_embed=lambda query: iter([sparse]))
        self.retriever = HybridRetriever(
            config={
                "k": 2,
                "candidates": 3,
                "neighbors": 1,
                "context_tokens": 20000,
                "translate": True,
                "rerank": True,
                "show_branches": True,
                "query_template": "Query: {query}",
            },
            dim=2,
            collection="test_chunks",
            qdrant=self.client,
            embedder=self.api,
            generator=self.api,
            reranker=self.api,
            bm25=bm25,
            answer_prompt="Question: {question}\nSources:\n{context}",
        )
        with __import__("warnings").catch_warnings():
            __import__("warnings").simplefilter("ignore", UserWarning)
            prepare_collection(self.client, "test_chunks", 2)
        self.chunks = [
            {
                "chunk_id": "doc:00000",
                "text": "Backups are retained for 30 days.",
                "headings": ["Backups"],
                "path": "guide.md",
                "page": 1,
            },
            {
                "chunk_id": "doc:00001",
                "text": "Restoration is tested monthly.",
                "headings": ["Backups"],
                "path": "guide.md",
                "page": 1,
            },
            {
                "chunk_id": "doc:00002",
                "text": "VPN is required.",
                "headings": ["Access"],
                "path": "guide.md",
                "page": 2,
            },
        ]
        points = [
            qm.PointStruct(
                id=point_id(c["chunk_id"]),
                vector={"dense": [1.0, 0.0], "bm25": qm.SparseVector(indices=[1], values=[1.0])},
                payload=c,
            )
            for c in self.chunks
        ]
        self.client.upsert("test_chunks", points=points, wait=True)

    def tearDown(self):
        self.api.close()
        self.client.close()

    def test_inference_uses_both_queries_and_exact_numbered_context(self):
        result = self.retriever.answer_question("Как долго хранят резервные копии?")
        self.assertEqual(len(result["queries"]), 2)
        self.assertEqual({r["branch"] for r in result["branches"]}, {"dense", "bm25"})
        self.assertEqual(result["context"], "\n\n".join(result["retrieved_contexts"]))
        self.assertIn("30 days", result["context"])
        self.assertIn("[1]", result["response"])
        self.assertEqual(
            len(result["sources"]), 1
        )  # Повторный соседний чанк уже включён в контекст.
        with tempfile.TemporaryDirectory() as tmp:
            from helpers.artifacts import append_jsonl, latest_records

            path = Path(tmp) / "answers.jsonl"
            append_jsonl(path, {"id": "q1", "status": "ok", **result})
            self.assertEqual(
                latest_records(path)["q1"]["retrieved_contexts"], result["retrieved_contexts"]
            )

    def test_neighbors_do_not_cross_section_boundary(self):
        result = self.retriever.expand_neighbors([{"payload": self.chunks[1], "score": 0.5}])
        self.assertEqual(result[0]["chunk_ids"], ["doc:00000", "doc:00001"])
        self.assertNotIn("VPN", result[0]["text"])

    def test_context_budget_preserves_first_and_drops_later_items(self):
        items = [{"ref": str(i), "text": "x" * 100, "path": None, "page": None} for i in range(2)]
        context, selected, used = build_context(items, 1)
        self.assertEqual(len(selected), 1)
        self.assertEqual(used, 25)
        self.assertEqual(context, selected[0]["context_block"])

    def test_dimension_mismatch_rejected_without_recreating_collection(self):
        with self.assertRaisesRegex(ValueError, "mismatch"):
            prepare_collection(self.client, "test_chunks", 3)
        self.assertEqual(self.client.count("test_chunks", exact=True).count, 3)

    def test_reranker_failure_keeps_rrf_results(self):
        from types import SimpleNamespace

        def fail(*args):
            raise RuntimeError("offline")

        self.retriever.reranker = SimpleNamespace(rerank=fail)
        # assertWarns обходит загруженные модули и может запустить отложенные импорты transformers.
        import warnings

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", UserWarning)
            hits, candidates, _, _ = self.retriever.query_hits("backup retention")
        self.assertTrue(any("Reranker failed" in str(item.message) for item in caught))
        self.assertEqual(len(hits), 2)
        self.assertTrue(all(h["score"] == h["rrf_score"] for h in candidates))

    def test_empty_search_returns_without_generation(self):
        self.client.delete_collection("test_chunks")
        with __import__("warnings").catch_warnings():
            __import__("warnings").simplefilter("ignore", UserWarning)
            prepare_collection(self.client, "test_chunks", 2)
        self.retriever.config["translate"] = False
        result = self.retriever.answer_question("unknown")
        self.assertEqual(result["retrieved_contexts"], [])
        self.assertNotIn("/v1/chat/completions", self.api_calls)

    def test_generation_reuses_inspected_context_without_searching_again(self):
        found = self.retriever.search("backup retention")
        before = len(self.api_calls)
        result = self.retriever.answer_from_search(found)
        self.assertEqual(self.api_calls[before:], ["/v1/chat/completions"])
        self.assertEqual(result["context"], found["context"])
        self.assertEqual(result["retrieved_contexts"], found["retrieved_contexts"])

    def test_single_branch_modes_and_same_candidate_pool_without_reranker(self):
        for mode in ("dense", "bm25", "hybrid"):
            self.retriever.config.update(mode=mode, translate=False)
            ranked = self.retriever.search("backup retention")
            self.retriever.config["rerank"] = False
            plain = self.retriever.search("backup retention")
            self.assertEqual(
                {h["payload"]["chunk_id"] for h in ranked["candidates"]},
                {h["payload"]["chunk_id"] for h in plain["candidates"]},
            )
            branches = {r["branch"] for r in ranked["branches"]}
            self.assertEqual(branches, {"dense", "bm25"} if mode == "hybrid" else {mode})
            self.retriever.config["rerank"] = True


class EvaluationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from types import SimpleNamespace

        class Metric:
            async def ascore(self, **kwargs):
                self.kwargs = kwargs
                return SimpleNamespace(value=0.75)

        self.metric = Metric()
        self.row = {
            "id": "q",
            "question": "How long?",
            "response": "30 days.",
            "retrieved_contexts": ["[1] Retain for 30 days."],
            "reference": "30 days.",
            "status": "ok",
        }

    async def test_metric_dispatch_and_saved_exact_contexts(self):
        for name in [
            "faithfulness",
            "answer_relevancy",
            "context_precision_with_reference",
            "context_recall",
            "factual_correctness",
        ]:
            result = await evaluate_one(self.row, name, self.metric)
            self.assertEqual(result, {"status": "ok", "value": 0.75})
            if name in CONTEXT_METRICS:
                self.assertEqual(
                    self.metric.kwargs["retrieved_contexts"], self.row["retrieved_contexts"]
                )

    async def test_missing_reference_empty_context_and_failed_inference(self):
        for row, name, reason in [
            ({**self.row, "reference": None}, "context_recall", "no_reference"),
            ({**self.row, "retrieved_contexts": []}, "faithfulness", "empty_context"),
            ({**self.row, "status": "error"}, "answer_relevancy", "inference_failed_or_missing"),
        ]:
            result = await evaluate_one(row, name, self.metric)
            self.assertEqual(result["status"], "skipped")
            self.assertEqual(result["reason"], reason)

    async def test_undefined_and_errors_are_not_zero(self):
        from types import SimpleNamespace

        class Undefined:
            async def ascore(self, **kwargs):
                return SimpleNamespace(value=float("nan"))

        class Failure:
            async def ascore(self, **kwargs):
                raise RuntimeError("judge unavailable")

        for metric, status in [(Undefined(), "undefined"), (Failure(), "error")]:
            result = await evaluate_one(self.row, "faithfulness", metric)
            self.assertEqual(result["status"], status)
            self.assertIsNone(result["value"])

    async def test_authentication_error_stops_batch_and_keeps_checkpoint(self):
        import contextlib
        import io

        import httpx
        from helpers.artifacts import append_jsonl, ensure_manifest, latest_records
        from helpers.evaluation import EvaluationLab
        from openai import AuthenticationError

        class Unauthorized:
            async def ascore(self, **kwargs):
                response = httpx.Response(401, request=httpx.Request("POST", "http://judge/v1"))
                try:
                    raise AuthenticationError("Invalid API key", response=response, body=None)
                except AuthenticationError as exc:
                    raise RuntimeError("Judge retries exhausted") from exc

        with tempfile.TemporaryDirectory() as tmp:
            lab = EvaluationLab(tmp, run_name="auth")
            samples = [
                {"id": key, "question": "How long?", "reference": "30 days."}
                for key in ("q1", "q2")
            ]
            ensure_manifest(lab.run_dir / "run.json", {"questions": samples})
            for sample in samples:
                append_jsonl(
                    lab.run_dir / "answers.jsonl",
                    {**self.row, **sample, "context": "[1] Retain for 30 days."},
                )
            lab.metrics = {"faithfulness": Unauthorized()}
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                with self.assertRaisesRegex(RuntimeError, "JUDGE_API_KEY"):
                    await lab.score(metric_names=["faithfulness"])
                saved = latest_records(lab.eval_dir / "scores.jsonl", ("id", "metric"))
                self.assertEqual(len(saved), 1)
                self.assertEqual(saved[("q1", "faithfulness")]["reason"], "authentication_error")
                self.assertTrue((lab.eval_dir / "summary.csv").exists())
                lab.metrics = {"faithfulness": self.metric}
                _, summary = await lab.score(metric_names=["faithfulness"])
            part = summary[summary.metric == "faithfulness"].iloc[0]
            self.assertEqual(part["ok"], 2)
            self.assertEqual(part["error"], 0)

    async def test_report_exposes_missing_and_invalidates_stale_scores(self):
        from helpers.artifacts import fingerprint

        questions = [
            {"id": "q", "question": "How long?"},
            {"id": "missing", "question": "Missing?"},
        ]
        scores = {
            ("q", "faithfulness"): {
                "status": "ok",
                "value": 0.9,
                "answer_fingerprint": fingerprint(self.row),
            }
        }
        _, summary = score_summary(questions, {"q": self.row}, scores, ["faithfulness"])
        self.assertEqual(summary.iloc[0]["ok"], 1)
        self.assertEqual(summary.iloc[0]["missing"], 1)
        changed = {**self.row, "status": "error"}
        _, summary = score_summary(questions, {"q": changed}, scores, ["faithfulness"])
        self.assertEqual(summary.iloc[0]["ok"], 0)
        self.assertEqual(summary.iloc[0]["missing"], 2)

    async def test_real_ragas_faithfulness_through_mock_http(self):
        import os

        os.environ["RAGAS_DO_NOT_TRACK"] = "true"
        import httpx
        from openai import AsyncOpenAI
        from ragas.llms import llm_factory
        from ragas.metrics.collections import Faithfulness

        responses = [
            {"statements": ["Backups are retained for 30 days."]},
            {
                "statements": [
                    {
                        "statement": "Backups are retained for 30 days.",
                        "reason": "The context explicitly states this.",
                        "verdict": 1,
                    }
                ]
            },
        ]

        def handler(request):
            return httpx.Response(
                200,
                json={
                    "id": "test",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "judge",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(responses.pop(0)),
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )

        async with AsyncOpenAI(
            api_key="test",
            base_url="http://test/v1",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ) as client:
            metric = Faithfulness(llm=llm_factory("judge", client=client, temperature=0))
            result = await evaluate_one(self.row, "faithfulness", metric)
            self.assertEqual(result, {"status": "ok", "value": 1.0})


class ParsingTests(unittest.TestCase):
    def test_text_formats_parse_without_model_downloads(self):
        from docling_core.transforms.chunker.tokenizer.base import BaseTokenizer
        from helpers.parsing import DocumentParser, chunk_document

        class Tokenizer(BaseTokenizer):
            def count_tokens(self, text):
                return len(text.split())

            def get_max_tokens(self):
                return 512

            def get_tokenizer(self):
                return self.count_tokens

        parser = DocumentParser(ocr=False)
        with tempfile.TemporaryDirectory() as tmp:
            examples = {
                "sample.txt": "Keep backups for 30 days.\n\nTest monthly.",
                "sample.md": "# Backups\n\nKeep backups for 30 days.",
                "sample.adoc": "= Backups\n\nKeep backups for 30 days.",
                "sample.html": "<html><body><h1>Backups</h1><p>Keep backups for 30 days.</p></body></html>",
                "sample.csv": "policy,days\nbackups,30\n",
            }
            for name, text in examples.items():
                with self.subTest(name=name):
                    path = Path(tmp) / name
                    path.write_text(text, encoding="utf-8")
                    doc = parser.parse(path)
                    chunks = chunk_document(doc, path=name, doc_id="test", tokenizer=Tokenizer())
                    self.assertTrue(chunks)
                    self.assertIn("30", "\n".join(c["text"] for c in chunks))

    def test_empty_folder_is_rejected(self):
        from helpers.parsing import discover_documents

        with (
            tempfile.TemporaryDirectory() as tmp,
            self.assertRaisesRegex(ValueError, "no supported documents"),
        ):
            discover_documents(Path(tmp))

    def test_empty_vlm_description_is_an_error(self):
        from types import SimpleNamespace

        from helpers.parsing import describe_image
        from PIL import Image

        api = SimpleNamespace(chat=lambda *a, **k: {"choices": [{"message": {"content": ""}}]})
        with self.assertRaisesRegex(ValueError, "empty"):
            describe_image(Image.new("RGB", (10, 10)), api)


class RecoveryRegressionTests(unittest.TestCase):
    def test_tokenizer_calls_server_root(self):
        import httpx
        from helpers.model_clients import ModelAPI
        from helpers.parsing import make_tokenizer

        urls = []

        def handler(request):
            urls.append(str(request.url))
            return httpx.Response(200, json={"tokens": [1, 2]})

        with ModelAPI(
            "http://test/prefix/v1", "embed", transport=httpx.MockTransport(handler)
        ) as api:
            tokenizer = make_tokenizer(api)
            self.assertEqual(tokenizer.count_tokens("text"), 2)
            self.assertEqual(urls, ["http://test/prefix/tokenize"] * 2)
            tokenizer.count_tokens("text")
            self.assertEqual(len(urls), 2)

    def test_interrupted_utf8_tail_recovers(self):
        from helpers.artifacts import append_jsonl, latest_records

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "answers.jsonl"
            path.write_bytes(
                b'{"id":"one","status":"ok"}\n' + '{"id":"two","response":"Я'.encode()[:-1]
            )
            self.assertEqual(list(latest_records(path)), ["one"])
            append_jsonl(path, {"id": "two", "status": "ok"})
            self.assertEqual(len(latest_records(path)), 2)
