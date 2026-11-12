"""Загрузка и подстановка промптов из `src/mnogobase/llm/prompts/*.md`.

Шаблоны — `string.Template`: плейсхолдеры `$name` / `${name}`, литеральный `$` пишется как
`$$`. Схемы JSON-ответов в промптах не описываются — их задаёт код (pydantic).

Маркер недоверенных данных. Каждый промпт для `render_messages` содержит отдельную строку
`<!-- untrusted-data -->`. Шаблон делится по ней ДО подстановки значений:

- часть выше маркера — правила приложения; она становится `system`-сообщением, перед ней
  добавляется общая политика `untrusted_data.md`. Здесь допустимы только настройки
  оператора (язык, типы сущностей);
- часть ниже маркера — данные: тексты документов, вопросы, факты графа, существующие
  wiki-страницы, прежние ответы модели. Она становится `user`-сообщением.

Поскольку деление происходит до подстановки, текст документа, даже содержащий сам маркер или
«system:», не может попасть в `system` и создать новую роль. Это снижает риск
prompt injection, но не гарантирует послушание модели. Новый плейсхолдер нужно передать в
месте вызова `render(...)` / `render_messages(...)` и покрыть в `tests/unit/test_templates.py`.
"""

from __future__ import annotations

from importlib.resources import files
from string import Template

from mnogobase.llm.client import Message

# строка-разделитель правил (system) и данных (user) в файле промпта
_DATA_BOUNDARY = "\n<!-- untrusted-data -->\n"


def render_messages(name: str, /, **values: object) -> list[Message]:
    """Собирает сообщения `[system, user]` из промпта `prompts/<name>.md`.

    Шаблон делится по маркеру до подстановки, поэтому данные не могут создать роли.
    Выше маркера — только настройки, которые контролирует оператор. Поля документов и
    запросов, включая метаданные и прежние ответы модели, должны оставаться ниже него.
    Разделение ролей снижает риск инъекций, но не гарантирует, что модель подчинится.

    Подстановка строгая (`Template.substitute`): отсутствующее значение даёт `KeyError`.
    Значения приводятся к `str`. Промпт без маркера — `ValueError`.
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
    """Текст промпта `prompts/<name>.md` целиком, без деления на роли.

    Подстановка мягкая (`safe_substitute`): неизвестные `$name` остаются как есть. Сейчас
    используется для общей политики `untrusted_data.md`."""
    # `name` только позиционный, чтобы в шаблонах можно было использовать плейсхолдер `$name`.
    text = (files("mnogobase.llm") / "prompts" / f"{name}.md").read_text(encoding="utf-8")
    return Template(text).safe_substitute({k: str(v) for k, v in values.items()})
