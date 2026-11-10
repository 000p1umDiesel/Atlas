"""Проверки helpers без моделей и сети: python -m unittest discover -s tests"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from helpers.clients import ModelAPI  # noqa: E402
from helpers.corpus import load_questions  # noqa: E402
from helpers.evaluation import mean_scores, score_table, status_counts  # noqa: E402
from helpers.retrieval import chunk_text, point_id  # noqa: E402
from helpers.settings import server_commands  # noqa: E402
from helpers.storage import append_jsonl, check_config, latest_records, read_jsonl  # noqa: E402


class StorageTests(unittest.TestCase):
    def test_config_is_written_once_and_then_compared(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run" / "config.json"
            check_config(path, {"k": 8, "mode": "hybrid"})
            check_config(path, {"k": 8, "mode": "hybrid"})
            with self.assertRaisesRegex(ValueError, "k"):
                check_config(path, {"k": 4, "mode": "hybrid"})

    def test_interrupted_last_line_is_ignored_and_retry_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "answers.jsonl"
            append_jsonl(path, {"id": "a", "status": "error"})
            append_jsonl(path, {"id": "a", "status": "ok"})
            with path.open("a") as stream:
                stream.write('{"id": "b", "sta')
            self.assertEqual(len(read_jsonl(path)), 2)
            self.assertEqual(latest_records(path)["a"]["status"], "ok")

    def test_broken_line_in_the_middle_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scores.jsonl"
            path.write_text('{"id": 1}\nnot json\n{"id": 2}\n')
            with self.assertRaises(json.JSONDecodeError):
                read_jsonl(path)


class CorpusTests(unittest.TestCase):
    def test_question_set_shape(self):
        questions = load_questions(ROOT / "data" / "questions.jsonl")
        self.assertEqual(len(questions), 40)
        self.assertEqual(sum(bool(q.get("reference")) for q in questions), 36)
        for q in questions:
            if q.get("reference"):
                self.assertTrue(q["source_pages"], q["id"])
            else:
                self.assertTrue(q["expected_behavior"], q["id"])

    def test_duplicate_ids_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "q.jsonl"
            path.write_text('{"id": "x", "question": "a"}\n{"id": "x", "question": "b"}\n')
            with self.assertRaisesRegex(ValueError, "повторяется"):
                load_questions(path)


class ClientTests(unittest.TestCase):
    def make_api(self, handler):
        return ModelAPI("http://test/v1", "m", transport=httpx.MockTransport(handler))

    def test_embeddings_are_normalized_and_ordered(self):
        def handler(request):
            data = [{"index": 1, "embedding": [0, 2]}, {"index": 0, "embedding": [3, 4]}]
            return httpx.Response(200, json={"data": data})

        with self.make_api(handler) as api:
            self.assertEqual(api.embed(["a", "b"]), [[0.6, 0.8], [0.0, 1.0]])

    def test_rerank_scores_follow_input_order(self):
        def handler(request):
            results = [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.1}]
            return httpx.Response(200, json={"results": results})

        with self.make_api(handler) as api:
            self.assertEqual(api.rerank("q", ["a", "b"]), [0.1, 0.9])

    def test_missing_model_is_reported(self):
        def handler(request):
            return httpx.Response(200, json={"data": [{"id": "other"}]})

        with self.make_api(handler) as api, self.assertRaisesRegex(ValueError, "other"):
            api.require_model()

    def test_client_errors_are_not_retried(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(401)

        with self.make_api(handler) as api, self.assertRaises(httpx.HTTPStatusError):
            api.chat([])
        self.assertEqual(len(calls), 1)


class SettingsTests(unittest.TestCase):
    def test_commands_only_for_local_servers(self):
        config = {
            "embed_url": "http://localhost:8101/v1",
            "embed_model": "e",
            "vision_url": "http://localhost:8103/v1",
            "vision_model": "v",
            "rerank_url": "https://api.example.com/v1",
            "rerank_model": "r",
        }
        commands = server_commands(config)
        self.assertEqual(set(commands), {"embed", "vision"})
        self.assertIn("--port 8101", commands["embed"])
        self.assertIn("--image-min-tokens 1024", commands["vision"])


class RetrievalTests(unittest.TestCase):
    def test_point_id_is_stable_uuid(self):
        self.assertEqual(point_id("doc:00001"), point_id("doc:00001"))
        self.assertNotEqual(point_id("doc:00001"), point_id("doc:00002"))

    def test_chunk_text_prefixes_headings(self):
        payload = {"headings": ["3 Model", "3.2 Attention"], "text": "body"}
        self.assertEqual(chunk_text(payload), "3 Model > 3.2 Attention\nbody")
        self.assertEqual(chunk_text({"text": "body"}), "body")


class EvaluationTests(unittest.TestCase):
    def test_means_skip_missing_and_errors(self):
        questions = [{"id": "a", "reference": "r"}, {"id": "b"}]
        scores = {
            ("a", "faithfulness"): {"status": "ok", "value": 1.0},
            ("b", "faithfulness"): {"status": "error", "error": "timeout"},
            ("b", "context_recall"): {"status": "skipped", "reason": "нет эталона"},
        }
        table = score_table(questions, scores, run="r")
        self.assertEqual(len(table), 10)
        self.assertEqual(mean_scores(table).loc["r", "faith"], 1.0)
        counts = status_counts(table)
        self.assertEqual(counts.loc["faithfulness", "error"], 1)
        self.assertEqual(counts.loc["context_recall", "skipped"], 1)


if __name__ == "__main__":
    unittest.main()
