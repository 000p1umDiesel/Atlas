"""Гибридный поиск, добавление соседних чанков и генерация по найденным источникам."""

from __future__ import annotations

import time
import warnings

from qdrant_client import models as qm


def point_id(chunk_id):
    from uuid import NAMESPACE_URL, uuid5

    return str(uuid5(NAMESPACE_URL, chunk_id))


def sparse_vector(embedding):
    return qm.SparseVector(indices=embedding.indices.tolist(), values=embedding.values.tolist())


def chunk_text(payload):
    headings = payload.get("headings") or []
    return (" > ".join(headings) + "\n" if headings else "") + payload["text"]


def prepare_collection(client, name, dim):
    if client.collection_exists(name):
        params = client.get_collection(name).config.params
        dense = params.vectors.get("dense") if isinstance(params.vectors, dict) else None
        sparse = (params.sparse_vectors or {}).get("bm25")
        if (
            dense is None
            or dense.size != dim
            or dense.distance != qm.Distance.COSINE
            or (sparse is None)
            or (sparse.modifier != qm.Modifier.IDF)
        ):
            raise ValueError("Collection schema/dimension mismatch; use a new INDEX_NAME")
    else:
        client.create_collection(
            name,
            vectors_config={"dense": qm.VectorParams(size=dim, distance=qm.Distance.COSINE)},
            sparse_vectors_config={"bm25": qm.SparseVectorParams(modifier=qm.Modifier.IDF)},
        )
        client.create_payload_index(name, "chunk_id", field_schema=qm.PayloadSchemaType.KEYWORD)


def build_context(items, max_tokens):
    """Нумеруем источники; бюджет приблизительный, по оценке «символы / 4»."""
    blocks, selected, used = ([], [], 0)
    for item in items:
        cost = max(1, len(item["text"]) // 4)
        if selected and used + cost > max_tokens:
            break
        n = len(selected) + 1
        label = f"document {item['path'] or item['ref']}"
        if item.get("page") is not None:
            label += f", p.{item['page']}"
        block = f"[{n}] ({label})\n{item['text']}"
        blocks.append(block)
        selected.append({**item, "n": n, "context_block": block})
        used += cost
    return ("\n\n".join(blocks), selected, used)


class HybridRetriever:
    def __init__(
        self, *, config, dim, collection, qdrant, embedder, generator, reranker, bm25, answer_prompt
    ):
        self.config = dict(config)
        self.dim = dim
        self.collection = collection
        self.qdrant = qdrant
        self.embedder = embedder
        self.generator = generator
        self.reranker = reranker
        self.bm25 = bm25
        self.answer_prompt = answer_prompt
        self.translation_cache = {}

    def translate_question(self, question):
        letters = [c for c in question if c.isalpha()]
        if (
            not self.config["translate"]
            or not letters
            or sum(not c.isascii() for c in letters) / len(letters) <= 0.3
        ):
            return []
        if question not in self.translation_cache:
            try:
                response = self.generator.chat(
                    [
                        {
                            "role": "user",
                            "content": "Translate the question to English for document retrieval. Return only the translation.\n"
                            + question,
                        }
                    ],
                    temperature=0,
                )
                text = response["choices"][0]["message"]["content"].strip().splitlines()[0].strip()
                self.translation_cache[question] = (
                    [text] if text.casefold() != question.casefold() else []
                )
            except Exception as exc:
                warnings.warn(
                    f"Translation failed; original query only: {type(exc).__name__}: {exc}",
                    stacklevel=2,
                )
                self.translation_cache[question] = []
        return self.translation_cache[question]

    def query_hits(self, question):
        """Передаём в RRF Qdrant списки dense и BM25 для каждой формулировки вопроса."""
        queries = list(dict.fromkeys([question, *self.translate_question(question)]))
        # Сохраняем одинаковый пул кандидатов при сравнении с reranker и без него.
        count = max(self.config["k"], self.config["candidates"])
        mode = self.config.get("mode", "hybrid")
        if mode not in ("hybrid", "dense", "bm25"):
            raise ValueError(f"Unknown retrieval mode: {mode}")
        prefetch, trace = ([], [])
        for query in queries:
            vectors = []
            if mode in ("hybrid", "dense"):
                dense = self.embedder.embed(
                    [self.config["query_template"].format(query=query)], dim=self.dim
                )[0]
                vectors.append(("dense", dense))
            if mode in ("hybrid", "bm25"):
                vectors.append(("bm25", sparse_vector(next(iter(self.bm25.query_embed(query))))))
            for name, vector in vectors:
                prefetch.append(qm.Prefetch(query=vector, using=name, limit=count * 4))
                if self.config["show_branches"]:
                    hits = self.qdrant.query_points(
                        self.collection,
                        query=vector,
                        using=name,
                        limit=count * 4,
                        with_payload=True,
                    ).points
                    trace.extend(
                        (
                            {
                                "query": query,
                                "branch": name,
                                "rank": i + 1,
                                "chunk_id": h.payload["chunk_id"],
                                "score": h.score,
                            }
                            for i, h in enumerate(hits)
                        )
                    )
        hits = self.qdrant.query_points(
            self.collection,
            prefetch=prefetch,
            query=qm.FusionQuery(fusion=qm.Fusion.RRF),
            limit=count,
            with_payload=True,
        ).points
        candidates = [
            {"payload": h.payload, "rrf_score": h.score, "score": h.score, "rrf_rank": i + 1}
            for i, h in enumerate(hits)
        ]
        if self.config["rerank"] and candidates:
            try:
                scores = self.reranker.rerank(
                    question, [chunk_text(h["payload"]) for h in candidates]
                )
                for hit, score in zip(candidates, scores, strict=True):
                    hit["score"] = score
                    hit["rerank_score"] = score
                candidates.sort(key=lambda h: -h["score"])
            except Exception as exc:
                warnings.warn(
                    f"Reranker failed; using RRF order: {type(exc).__name__}: {exc}", stacklevel=2
                )
        return (candidates[: self.config["k"]], candidates, trace, queries)

    def expand_neighbors(self, hits):
        """Объединяем соседние чанки одной секции, избегая повторов в контексте."""
        n = self.config["neighbors"]
        known = {h["payload"]["chunk_id"]: h["payload"] for h in hits}
        wanted = set()
        for hit in hits:
            payload = hit["payload"]
            doc, index = payload["chunk_id"].rsplit(":", 1)
            wanted.update(
                f"{doc}:{i:05d}" for i in range(max(0, int(index) - n), int(index) + n + 1)
            )
        if wanted:
            for point in self.qdrant.retrieve(
                self.collection, ids=[point_id(k) for k in sorted(wanted)], with_payload=True
            ):
                known[point.payload["chunk_id"]] = point.payload
        covered, contexts = (set(), [])
        for hit in hits:
            payload = hit["payload"]
            key = payload["chunk_id"]
            if key in covered:
                continue
            doc, index = key.rsplit(":", 1)
            block = [key]
            for step in (-1, 1):
                for i in range(int(index) + step, int(index) + step * (n + 1), step):
                    neighbor = f"{doc}:{i:05d}"
                    other = known.get(neighbor)
                    if (
                        i < 0
                        or other is None
                        or neighbor in covered
                        or ((other.get("headings") or []) != (payload.get("headings") or []))
                    ):
                        break
                    block = [neighbor, *block] if step < 0 else [*block, neighbor]
            covered.update(block)
            first = known[block[0]]
            headings = first.get("headings") or []
            text = (" > ".join(headings) + "\n" if headings else "") + "\n".join(
                known[k]["text"] for k in block
            )
            pages = [known[k]["page"] for k in block if known[k].get("page") is not None]
            contexts.append(
                {
                    "ref": key,
                    "chunk_ids": block,
                    "text": text,
                    "path": first.get("path"),
                    "page": min(pages) if pages else None,
                    "score": hit["score"],
                }
            )
        return contexts

    def search(self, question):
        """Находим кандидатов и собираем точный контекст, пока не генерируя ответ."""
        question = question.strip()
        if not question:
            raise ValueError("Введите непустой вопрос.")
        start = time.perf_counter()
        hits, candidates, branches, queries = self.query_hits(question)
        context, sources, used = build_context(
            self.expand_neighbors(hits), self.config["context_tokens"]
        )
        return {
            "question": question,
            "queries": queries,
            "candidates": candidates,
            "branches": branches,
            "sources": sources,
            "retrieved_contexts": [s["context_block"] for s in sources],
            "context": context,
            "context_tokens_estimate": used,
            "latency_ms": round((time.perf_counter() - start) * 1000),
        }

    def answer_from_search(self, result):
        """Генерируем ответ по уже просмотренному контексту, не повторяя поиск."""
        start = time.perf_counter()
        answer = "В базе знаний нет релевантных источников для этого вопроса."
        usage = {}
        if result["sources"]:
            prompt = self.answer_prompt.format(
                question=result["question"], context=result["context"]
            )
            completion = self.generator.chat([{"role": "user", "content": prompt}], temperature=0.1)
            answer = completion["choices"][0]["message"]["content"]
            if not answer or not answer.strip():
                raise ValueError("Generator returned an empty answer")
            usage = completion.get("usage", {})
        return {
            **result,
            "response": answer,
            "usage": usage,
            "latency_ms": result["latency_ms"] + round((time.perf_counter() - start) * 1000),
        }

    def answer_question(self, question):
        return self.answer_from_search(self.search(question))
