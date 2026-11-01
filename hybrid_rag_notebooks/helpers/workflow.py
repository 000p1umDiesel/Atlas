"""Этапы RAG: те же методы можно вызывать из обычных ячеек, без виджетов."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from qdrant_client import QdrantClient
from qdrant_client import models as qm
from tqdm.std import tqdm

from .artifacts import (
    append_jsonl,
    ensure_manifest,
    fingerprint,
    latest_records,
    load_questions,
    write_json,
)
from .model_clients import ModelAPI
from .parsing import (
    DocumentParser,
    chunk_document,
    discover_documents,
    document_inventory,
    make_tokenizer,
)
from .retrieval import HybridRetriever, point_id, prepare_collection, sparse_vector
from .settings import DEFAULT_ANSWER_PROMPT, rag_settings


class RAGLab:
    def __init__(
        self,
        root,
        *,
        config=None,
        index_name="demo_v1",
        collection="notebook_hybrid_chunks",
        documents_dir=None,
        answer_prompt=DEFAULT_ANSWER_PROMPT,
    ):
        self.root = Path(root)
        self.config = dict(config if config is not None else rag_settings())
        self.index_dir = self.root / "outputs" / "indexes" / index_name
        self.collection = collection
        self.documents_dir = (
            Path(documents_dir) if documents_dir else self.root / "data" / "documents"
        )
        self.answer_prompt = answer_prompt
        self.chunks = []
        self.parsed = False
        self.index_config = None
        self.connected = False
        self.indexed = False
        self.clients = {}
        self.qdrant = None

    def connect(self):
        """Проверяем API и токенизатор до долгого разбора и построения индекса."""
        from fastembed import SparseTextEmbedding

        self.close()
        cfg = self.config
        roles = [("embed", "EMBED_API_KEY"), ("llm", "LLM_API_KEY")]
        roles += [
            (role, key)
            for role, key in [("rerank", "RERANK_API_KEY"), ("vision", "VISION_API_KEY")]
            if cfg[role]
        ]
        checks = []
        try:
            for role, key in tqdm(roles, desc="Проверка моделей", unit="API"):
                client = ModelAPI(cfg[f"{role}_url"], cfg[f"{role}_model"], key)
                self.clients[role] = client
                models = [m["id"] for m in client.models()["data"]]
                if client.model not in models:
                    raise ValueError(
                        f"{role}: модель {client.model!r} не найдена; /models: {models}"
                    )
                checks.append({"сервис": role, "модель": client.model, "статус": "готов"})
            print("Проверяем размерность embeddings и /tokenize…", flush=True)
            self.dim = len(self.clients["embed"].embed(["dimension probe"])[0])
            self.tokenizer = make_tokenizer(
                self.clients["embed"], cfg["max_tokens"], cfg["tokenizer_hf"]
            )
            self.qdrant = QdrantClient(url=cfg["qdrant_url"], timeout=120)
            self.qdrant.get_collections()
            checks.append(
                {"сервис": "Qdrant", "модель": f"dense: {self.dim} измерений", "статус": "готов"}
            )
            print("Загружаем BM25 (при первом запуске — скачивание модели)…", flush=True)
            self.bm25 = SparseTextEmbedding(cfg["sparse_model"])
            self.connected = True
            return pd.DataFrame(checks)
        except BaseException:
            self.close()
            raise

    def _require_connection(self):
        if not self.connected:
            raise RuntimeError("Сначала выполните ячейку lab.connect().")

    def parse_documents(self):
        """Разбираем файлы и сохраняем чанки каждого документа для продолжения после сбоя."""
        self._require_connection()
        self.indexed = False
        self.parsed = False
        paths = discover_documents(self.documents_dir)
        inventory = document_inventory(paths, self.documents_dir)
        keys = (
            "embed_url",
            "embed_model",
            "doc_template",
            "query_template",
            "sparse_model",
            "max_tokens",
            "device",
            "ocr",
            "vision",
            "vision_url",
            "vision_model",
            "vision_min_area",
            "vision_skip",
            "tokenizer_hf",
        )
        self.index_config = {k: self.config[k] for k in keys}
        self.index_config.update(
            dimension=self.dim,
            corpus=inventory,
            collection=self.collection,
            qdrant_url=self.config["qdrant_url"],
            implementation=1,
        )
        ensure_manifest(self.index_dir / "index.json", self.index_config)
        parser = DocumentParser(
            ocr=self.config["ocr"],
            device=self.config["device"],
            vision=self.clients.get("vision"),
            min_area=self.config["vision_min_area"],
            skip=self.config["vision_skip"],
        )
        chunks, errors = [], []
        cached = sum(
            (self.index_dir / "parsed" / f"{fingerprint(e)[:16]}.json").exists() for e in inventory
        )
        print(
            f"Файлов: {len(paths)}; из checkpoint: {cached}; осталось разобрать: {len(paths) - cached}",
            flush=True,
        )
        progress = tqdm(
            list(zip(paths, inventory, strict=True)), desc="Разбор → чанки", unit="файл"
        )
        for path, entry in progress:
            progress.set_postfix(file=entry["path"], errors=len(errors))
            doc_id = fingerprint(entry)[:16]
            cache = self.index_dir / "parsed" / f"{doc_id}.json"
            try:
                if cache.exists():
                    records = json.loads(cache.read_text(encoding="utf-8"))
                else:
                    records = chunk_document(
                        parser.parse(path),
                        path=entry["path"],
                        doc_id=doc_id,
                        tokenizer=self.tokenizer,
                    )
                    write_json(cache, records)
                chunks.extend(records)
            except Exception as exc:
                errors.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
                tqdm.write(str(errors[-1]))
        self.chunks = chunks
        if errors:
            raise RuntimeError(
                f"Не удалось разобрать файлов: {len(errors)}. Исправьте ошибки и повторите шаг; готовые файлы сохранены."
            )
        if not chunks:
            raise ValueError("Документы не дали ни одного чанка.")
        print(f"Готово: {len(paths)} документов → {len(chunks)} чанков.")
        self.parsed = True
        return chunks

    def build_index(self):
        """Записываем недостающие векторы и восстанавливаем удалённые точки коллекции."""
        self._require_connection()
        self.indexed = False
        if not self.parsed or not self.chunks or self.index_config is None:
            raise RuntimeError("Сначала выполните ячейку lab.parse_documents().")
        existing = self.qdrant.collection_exists(self.collection)
        if existing and not (self.index_dir / "binding.json").exists():
            raise ValueError(
                "Collection already exists without this notebook's binding; choose a new COLLECTION"
            )
        ensure_manifest(self.index_dir / "binding.json", self.index_config)
        prepare_collection(self.qdrant, self.collection, self.dim)
        finished = latest_records(self.index_dir / "indexed.jsonl") if existing else {}
        expected = [point_id(c["chunk_id"]) for c in self.chunks]
        present = set()
        for start in tqdm(range(0, len(expected), 256), desc="Проверка checkpoint", unit="пакет"):
            present.update(
                str(p.id)
                for p in self.qdrant.retrieve(
                    self.collection, ids=expected[start : start + 256], with_payload=False
                )
            )
        pending = [
            c
            for c in self.chunks
            if finished.get(c["chunk_id"], {}).get("status") != "ok"
            or point_id(c["chunk_id"]) not in present
        ]
        print(
            f"Уже в индексе: {len(self.chunks) - len(pending)}; осталось: {len(pending)}",
            flush=True,
        )
        for start in tqdm(
            range(0, len(pending), self.config["batch_size"]),
            desc="Embeddings + запись",
            unit="пакет",
        ):
            batch = pending[start : start + self.config["batch_size"]]
            dense = self.clients["embed"].embed(
                [
                    self.config["doc_template"].format(text=c["text"], title=Path(c["path"]).stem)
                    for c in batch
                ],
                dim=self.dim,
            )
            sparse = [sparse_vector(e) for e in self.bm25.passage_embed([c["text"] for c in batch])]
            points = [
                qm.PointStruct(
                    id=point_id(c["chunk_id"]), vector={"dense": d, "bm25": s}, payload=c
                )
                for c, d, s in zip(batch, dense, sparse, strict=True)
            ]
            self.qdrant.upsert(self.collection, points=points, wait=True)
            for chunk in batch:
                append_jsonl(
                    self.index_dir / "indexed.jsonl", {"id": chunk["chunk_id"], "status": "ok"}
                )
        count = self.qdrant.count(self.collection, exact=True).count
        if count != len(self.chunks):
            raise ValueError(
                f"Unexpected collection size {count}; expected {len(self.chunks)}. Use a fresh collection."
            )
        self.indexed = True
        print(f"Индекс готов: {count} чанков. Можно задавать вопросы.")
        return pd.DataFrame(
            [{"коллекция": self.collection, "чанков": count, "dense + BM25": "готово"}]
        )

    def retriever(self, **overrides):
        self._require_connection()
        if not self.indexed:
            raise RuntimeError("Сначала выполните ячейку lab.build_index().")
        config = {**self.config, **overrides}
        if config["rerank"] and "rerank" not in self.clients:
            raise RuntimeError(
                "Reranker отключён в CFG. Включите его и повторите подключение или задайте SEARCH['rerank'] = False."
            )
        return HybridRetriever(
            config=config,
            dim=self.dim,
            collection=self.collection,
            qdrant=self.qdrant,
            embedder=self.clients["embed"],
            generator=self.clients["llm"],
            reranker=self.clients.get("rerank"),
            bm25=self.bm25,
            answer_prompt=self.answer_prompt,
        )

    def search(self, question, **overrides):
        return self.retriever(**overrides).search(question)

    def answer_from_search(self, result):
        return self.retriever().answer_from_search(result)

    def answer_question(self, question, **overrides):
        return self.retriever(**overrides).answer_question(question)

    def run_batch(self, run_name="baseline_v1", questions_path=None, **overrides):
        """Отвечаем на весь набор; эталоны сохраняем для проверки, не передавая генератору."""
        retriever = self.retriever(**overrides)
        questions = load_questions(
            Path(questions_path) if questions_path else self.root / "data" / "questions.jsonl"
        )
        run_dir = self.root / "outputs" / "runs" / run_name
        config = {
            "index": self.index_config,
            "retrieval": retriever.config,
            "answer_prompt": self.answer_prompt,
            "questions": questions,
            "implementation": 2,
        }
        ensure_manifest(run_dir / "run.json", config)
        path = run_dir / "answers.jsonl"
        previous = latest_records(path)
        pending = [q for q in questions if previous.get(q["id"], {}).get("status") != "ok"]
        print(
            f"Вопросов: {len(questions)}; уже готовы: {len(questions) - len(pending)}; осталось: {len(pending)}",
            flush=True,
        )
        for sample in tqdm(pending, desc="Ответы RAG", unit="вопрос"):
            try:
                # В поиск уходит только вопрос; reference и метаданные добавляются после ответа.
                result = retriever.answer_question(sample["question"])
                result.pop("candidates")
                result.pop("branches")
                row = {**sample, **result, "status": "ok"}
            except Exception as exc:
                row = {**sample, "status": "error", "error": f"{type(exc).__name__}: {exc}"}
                tqdm.write(f"{sample['id']}: {row['error']}")
            append_jsonl(path, row)
        latest = latest_records(path)
        failures = [q["id"] for q in questions if latest.get(q["id"], {}).get("status") != "ok"]
        print(
            f"Ответы: {path}\nГотово: {len(questions) - len(failures)}; ошибок/пропусков: {len(failures)}"
        )
        if failures:
            raise RuntimeError(
                f"Повторите после исправления ошибок: {failures}. Готовые ответы сохранены."
            )
        return pd.DataFrame(
            [
                {
                    "id": q["id"],
                    "вопрос": q["question"],
                    "ответ": latest[q["id"]]["response"],
                    "статус": "ok",
                }
                for q in questions
            ]
        )

    def close(self):
        self.connected = False
        self.indexed = False
        for client in self.clients.values():
            client.close()
        self.clients = {}
        if self.qdrant is not None:
            self.qdrant.close()
            self.qdrant = None
