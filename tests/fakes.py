from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable

from qdrant_client import models as qm

from mnogobase.llm.client import Usage

VOCAB = {
    "transformer": (
        "Transformer",
        "Method",
        "Neural network architecture based entirely on attention.",
    ),
    "attention": (
        "Attention Mechanism",
        "Method",
        "Lets a model focus on relevant parts of its input.",
    ),
    "внимани": (
        "Attention Mechanism",
        "Method",
        "Lets a model focus on relevant parts of its input.",
    ),
    "softmax": ("Softmax", "Concept", "Function that turns scores into probabilities."),
    "vaswani": ("Ashish Vaswani", "Person", "Researcher, first author of the Transformer paper."),
}
_EVIDENCE_ID = re.compile(r"\[([0-9a-f]{16}:\d{5})\]")


class FakeLLM:
    """Deterministic LLMClient: `handler(task, prompt) -> reply text`."""

    def __init__(self, handler: Callable[[str, str], str]):
        self.handler = handler
        self.calls: list[tuple[str, str]] = []
        self.usage = Usage()

    def model_for(self, task: str) -> str:
        return f"fake-{task}"

    def calls_for(self, task: str) -> list[str]:
        return [prompt for t, prompt in self.calls if t == task]

    def _reply(self, task: str, messages: list[dict[str, str]]) -> str:
        prompt = "\n".join(m["content"] for m in messages)
        self.calls.append((task, prompt))
        reply = self.handler(task, prompt)
        self.usage.tokens_in += len(prompt) // 4
        self.usage.tokens_out += len(reply) // 4
        return reply

    async def complete(self, messages, *, task: str) -> str:
        return self._reply(task, messages)

    async def structured(self, messages, schema, *, task: str, max_repairs: int = 2):
        return schema.model_validate_json(self._reply(task, messages))


def scripted_llm_handler(task: str, prompt: str) -> str:
    """Keyword-driven fake used by pipeline/wiki/retrieval tests."""
    if task == "extract":
        fragment = prompt.split("Fragment:", 1)[-1].casefold()
        entities: dict[str, dict] = {}
        for key, (name, etype, description) in VOCAB.items():
            if key in fragment and name not in entities:
                aliases = ["механизм внимания"] if key == "внимани" else []
                entities[name] = {
                    "name": name,
                    "type": etype,
                    "description": description,
                    "aliases": aliases,
                }
        names = list(entities)
        relations = [
            {
                "source": names[i],
                "target": names[i + 1],
                "predicate": "related_to",
                "description": f"{names[i]} relates to {names[i + 1]}.",
                "strength": 5,
            }
            for i in range(len(names) - 1)
        ]
        return json.dumps({"entities": list(entities.values()), "relations": relations})
    if task == "resolve":
        if "Merge these descriptions" in prompt:
            return json.dumps({"description": "Merged description."})
        return json.dumps({"same": False, "reason": "different things"})
    if task == "wiki":
        ids = _EVIDENCE_ID.findall(prompt)
        cite = f" [^{ids[0]}]" if ids else ""
        return (
            f"Summary sentence.{cite} See [[Softmax]] and [[Nonexistent Thing]].\n\n"
            f"## Details\nMore text.{cite}"
        )
    if task == "answer":
        return "The answer is supported by the sources [1]."
    raise AssertionError(f"unexpected task {task}")


_TOKEN = re.compile(r"\w+", re.UNICODE)


def _bucket(token: str, size: int) -> int:
    return int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16) % size


class FakeEmbedder:
    """Hashed bag-of-words: texts sharing words get high cosine similarity."""

    def __init__(self, dim: int = 64, templates: tuple[str, str] = ("{text}", "{query}")):
        self.dim = dim
        self.model_id = "fake-embed"
        self.templates = templates  # only signed (the index signature), never applied

    def _vec(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in _TOKEN.findall(text.casefold()):
            vec[_bucket(token, self.dim)] += 1.0
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]

    def embed_documents(self, items) -> list[list[float]]:
        return [self._vec(item.text or "") for item in items]

    def embed_query(self, query: str) -> list[float]:
        return self._vec(query)


class FakeSparse:
    def _sv(self, text: str) -> qm.SparseVector:
        counts: dict[int, float] = {}
        for token in _TOKEN.findall(text.casefold()):
            idx = _bucket(token, 1_000_003)
            counts[idx] = counts.get(idx, 0.0) + 1.0
        indices = sorted(counts)
        return qm.SparseVector(indices=indices, values=[counts[i] for i in indices])

    def encode_documents(self, texts) -> list[qm.SparseVector]:
        return [self._sv(t) for t in texts]

    def encode_query(self, text: str) -> qm.SparseVector:
        return self._sv(text)


class RecordingProgress:
    """ProgressSink that records every call as a tuple, for asserting the reported sequence."""

    def __init__(self):
        self.events: list[tuple] = []

    def files_found(self, total: int) -> None:
        self.events.append(("files", total))

    def file_started(self, path: str) -> None:
        self.events.append(("file", path))

    def file_done(self, status: str) -> None:
        self.events.append(("file_done", status))

    def step(self, name: str, total: int | None = None) -> None:
        self.events.append(("step", name, total))

    def advance(self, n: int = 1, *, failed: bool = False) -> None:
        self.events.append(("advance", n, failed))

    def steps(self) -> list[tuple]:
        return [e for e in self.events if e[0] == "step"]

    def advanced(self, step: str) -> list[tuple]:
        """The advances reported while the last `step(step, ...)` was current."""
        current, out = None, []
        for event in self.events:
            if event[0] == "step":
                current = event[1]
                if current == step:
                    out = []
            elif event[0] == "advance" and current == step:
                out.append(event)
        return out
