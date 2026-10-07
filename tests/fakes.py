from __future__ import annotations

import json
import re
from collections.abc import Callable

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
