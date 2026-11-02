from __future__ import annotations

from importlib.resources import files
from string import Template

from mnogobase.llm.client import Message

_DATA_BOUNDARY = "\n<!-- untrusted-data -->\n"


def render_messages(name: str, /, **values: object) -> list[Message]:
    """Split the source template before substitution, so data cannot create roles.

    Only operator-controlled settings belong above the boundary. Document/query
    fields, including metadata and earlier model output, must stay below it.
    Role separation reduces injection risk; it cannot guarantee model compliance.
    """
    template = (files("mnogobase.llm") / "prompts" / f"{name}.md").read_text(encoding="utf-8")
    rules, boundary, data = template.partition(_DATA_BOUNDARY)
    if not boundary:
        raise ValueError(f"Prompt {name!r} has no untrusted-data boundary")
    substitutions = {k: str(v) for k, v in values.items()}
    return [
        {
            "role": "system",
            "content": render("untrusted_data")
            + "\n\n"
            + Template(rules).substitute(substitutions),
        },
        {"role": "user", "content": Template(data).substitute(substitutions)},
    ]


def render(name: str, /, **values: object) -> str:
    # `name` только позиционный, чтобы в шаблонах можно было использовать плейсхолдер `$name`.
    text = (files("mnogobase.llm") / "prompts" / f"{name}.md").read_text(encoding="utf-8")
    return Template(text).safe_substitute({k: str(v) for k, v in values.items()})
