"""Проверки самих ноутбуков: функции поиска из ячеек 02 и чистота сохранённых выводов."""

import ast
import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from qdrant_client import QdrantClient
from qdrant_client import models as qm

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from helpers.retrieval import chunk_text, point_id, sparse_vector  # noqa: E402

NOTEBOOKS = ["01_indexing.ipynb", "02_retrieval_and_answer.ipynb", "03_evaluation.ipynb"]


def code_cells(name):
    notebook = json.loads((ROOT / name).read_text(encoding="utf-8"))
    return ["".join(c["source"]) for c in notebook["cells"] if c["cell_type"] == "code"]


def load_functions(name, namespace):
    """Выполняем из ячеек только определения функций, без вызовов моделей."""
    for source in code_cells(name):
        tree = ast.parse(source)
        defs = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
        if defs:
            exec(compile(ast.Module(body=defs, type_ignores=[]), name, "exec"), namespace)
    return namespace


class FakeEmbedder:
    """Вектор по наличию ключевых слов: достаточно, чтобы dense-поиск был детерминированным."""

    WORDS = ["head", "attention", "bleu", "train", "position"]

    def embed(self, texts):
        vectors = []
        for text in texts:
            vector = [float(word in text.lower()) for word in self.WORDS] + [0.01]
            norm = sum(x * x for x in vector) ** 0.5
            vectors.append([x / norm for x in vector])
        return vectors


class FakeBM25:
    def query_embed(self, text):
        words = sorted({w for w in text.lower().split() if w.isalpha()})
        indices = np.array([abs(hash(w)) % 10_000 for w in words])
        yield SimpleNamespace(indices=indices, values=np.ones(len(words)))

    def passage_embed(self, texts):
        for text in texts:
            yield next(self.query_embed(text))


class FakeReranker:
    def rerank(self, query, documents):
        return [float("BLEU" in d) + 1 / (i + 2) for i, d in enumerate(documents)]


CHUNKS = [
    ("Abstract", "The Transformer uses attention only."),
    ("3.2 Attention", "Multi-head attention with h = 8 heads."),
    ("3.2 Attention", "Each head has d_k = 64."),
    ("3.2 Attention", "Heads are concatenated."),
    ("5 Training", "We train on WMT 2014."),
    ("6 Results", "BLEU 28.4 on EN-DE."),
]


class RetrievalCellsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        qdrant = QdrantClient(":memory:")
        qdrant.create_collection(
            "test",
            vectors_config={"dense": qm.VectorParams(size=6, distance=qm.Distance.COSINE)},
            sparse_vectors_config={"bm25": qm.SparseVectorParams(modifier=qm.Modifier.IDF)},
        )
        embedder, bm25 = FakeEmbedder(), FakeBM25()
        chunks = [
            {
                "chunk_id": f"d:{i:05d}",
                "text": text,
                "headings": [heading],
                "page": i + 1,
                "path": "paper.pdf",
            }
            for i, (heading, text) in enumerate(CHUNKS)
        ]
        qdrant.upsert(
            "test",
            points=[
                qm.PointStruct(
                    id=point_id(c["chunk_id"]),
                    payload=c,
                    vector={
                        "dense": embedder.embed([c["text"]])[0],
                        "bm25": sparse_vector(next(bm25.passage_embed([c["text"]]))),
                    },
                )
                for c in chunks
            ],
        )
        cls.ns = load_functions(
            "02_retrieval_and_answer.ipynb",
            {
                "qdrant": qdrant,
                "embedder": embedder,
                "bm25": bm25,
                "reranker": FakeReranker(),
                "COLLECTION": "test",
                "INDEX_CONFIG": {"query_template": "{query}"},
                "qm": qm,
                "deepcopy": deepcopy,
                "chunk_text": chunk_text,
                "point_id": point_id,
                "sparse_vector": sparse_vector,
            },
        )
        cls.settings = {
            "mode": "hybrid",
            "candidates": 6,
            "rerank": True,
            "k": 2,
            "neighbors": 1,
            "context_tokens": 1000,
            "translate": False,
        }

    def test_qdrant_rrf_matches_formula_from_notebook(self):
        candidates, branches, _ = self.ns["retrieve_candidates"]("attention heads", self.settings)
        manual = {}
        for row in branches:
            manual[row["chunk_id"]] = manual.get(row["chunk_id"], 0) + 1 / (1 + row["rank"])
        for item in candidates:
            self.assertAlmostEqual(item["rrf_score"], manual[item["payload"]["chunk_id"]], places=5)

    def test_single_branch_modes(self):
        for mode in ("dense", "bm25"):
            _, branches, _ = self.ns["retrieve_candidates"](
                "attention", {**self.settings, "mode": mode}
            )
            self.assertEqual({b["branch"] for b in branches}, {mode})

    def test_reranker_reorders_without_mutating_input(self):
        candidates, _, _ = self.ns["retrieve_candidates"]("attention heads", self.settings)
        before = deepcopy(candidates)
        ranked = self.ns["rerank_candidates"]("q", candidates, True)
        self.assertEqual(candidates, before)
        self.assertIn("BLEU", ranked[0]["payload"]["text"])
        self.assertEqual(self.ns["rerank_candidates"]("q", candidates, False), candidates)

    def test_neighbors_stay_inside_section_and_do_not_repeat(self):
        hits = [
            {
                "payload": {
                    "chunk_id": "d:00002",
                    "headings": ["3.2 Attention"],
                    "text": CHUNKS[2][1],
                    "path": "paper.pdf",
                    "page": 3,
                },
                "score": 1,
            },
            {
                "payload": {
                    "chunk_id": "d:00003",
                    "headings": ["3.2 Attention"],
                    "text": CHUNKS[3][1],
                    "path": "paper.pdf",
                    "page": 4,
                },
                "score": 0.5,
            },
        ]
        items = self.ns["expand_neighbors"](hits, 2)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["chunk_ids"], ["d:00001", "d:00002", "d:00003"])
        self.assertEqual(items[0]["page"], 2)
        self.assertTrue(items[0]["text"].startswith("3.2 Attention\n"))

    def test_context_numbering_and_budget(self):
        items = [
            {"ref": f"d:{i}", "path": "paper.pdf", "page": i, "text": "x" * 400}
            for i in range(1, 4)
        ]
        context, sources, used = self.ns["build_context"](items, 200)
        self.assertEqual([s["n"] for s in sources], [1, 2])
        self.assertTrue(context.startswith("[1] (paper.pdf, p.1)"))
        self.assertEqual(used, 200)
        # первый блок попадает в контекст, даже если он сам больше бюджета
        _, sources, _ = self.ns["build_context"](items, 10)
        self.assertEqual(len(sources), 1)


class NotebookHygieneTests(unittest.TestCase):
    def test_code_cells_compile(self):
        for name in NOTEBOOKS:
            for source in code_cells(name):
                compile(source, name, "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)

    def test_outputs_have_no_local_paths_or_errors(self):
        for name in NOTEBOOKS:
            notebook = json.loads((ROOT / name).read_text(encoding="utf-8"))
            self.assertEqual(notebook["metadata"]["kernelspec"]["name"], "python3")
            for cell in notebook["cells"]:
                for output in cell.get("outputs", []):
                    self.assertNotEqual(output.get("output_type"), "error", name)
                    text = json.dumps(output, ensure_ascii=False)
                    self.assertNotIn("/Users/", text, name)
                    self.assertNotIn("/home/", text, name)

    def test_notebooks_do_not_import_parent_project(self):
        for name in NOTEBOOKS:
            self.assertNotIn("mnogobase", "\n".join(code_cells(name)), name)


if __name__ == "__main__":
    unittest.main()
