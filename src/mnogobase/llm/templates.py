from __future__ import annotations

from importlib.resources import files
from string import Template


def render(name: str, /, **values: object) -> str:
    # `name` только позиционный, чтобы в шаблонах можно было использовать плейсхолдер `$name`.
    text = (files("mnogobase.llm") / "prompts" / f"{name}.md").read_text(encoding="utf-8")
    return Template(text).safe_substitute({k: str(v) for k, v in values.items()})
