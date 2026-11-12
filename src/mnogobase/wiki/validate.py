"""Проверка тела wiki-страницы, написанного LLM, перед рендером.

Ответу модели не доверяем: он может процитировать чанк, которого не было в подтверждениях,
сослаться на несуществующую страницу или сам написать разделы, которые генерирует код.
Здесь:

- `strip_reserved_sections` вырезает заголовок `# `, разделы `## Related` / `## Sources` и
  определения сносок — их всегда пишет `render.render_page`;
- `validate_citations` оставляет только цитаты `[^chunk_id]` на разрешённые чанки;
- `resolve_links` переписывает `[[Имя|текст]]` в `[[slug|текст]]` для известных страниц,
  остальные ссылки превращает в простой текст.
"""

from __future__ import annotations

import re

from mnogobase.core.ids import normalize_name

# цитата `[^id]` вместе с одним пробелом перед ней, чтобы удаление не оставляло двойной пробел
_CITE = re.compile(r"[ \t]?\[\^([^\]\s]+)\]")
# "#" разрешён в цели ссылки: имена сущностей вроде "C#" сохраняют его после normalize_name
_LINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
# только точные сгенерированные заголовки: секция от LLM вроде "## Related work" остаётся
_RESERVED = re.compile(r"^##[ \t]+(Related|Sources)[ \t]*$", re.IGNORECASE | re.MULTILINE)
_FOOTNOTE_DEF = re.compile(r"^\[\^[^\]]+\]:.*$", re.MULTILINE)


def strip_reserved_sections(body: str) -> str:
    """Убирает начальный заголовок `# `, сгенерированные секции Related/Sources и определения
    сносок.

    Отрезается всё начиная с первого зарезервированного заголовка: код ставит Related и
    Sources в конец страницы, поэтому то, что за ними, тоже сгенерировано. Лишние пустые
    строки схлопываются до одной."""
    body = body.strip()
    if body.startswith("# "):
        body = body.split("\n", 1)[1] if "\n" in body else ""
    match = _RESERVED.search(body)
    if match:
        body = body[: match.start()]
    body = _FOOTNOTE_DEF.sub("", body)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def cited_ids(body: str) -> list[str]:
    """Id цитат `[^id]` из тела страницы без дублей, в порядке первого появления."""
    return list(dict.fromkeys(m.group(1) for m in _CITE.finditer(body)))


def validate_citations(body: str, allowed: set[str]) -> tuple[str, list[str]]:
    """Удаляет ссылки `[^id]` с неразрешёнными id; возвращает оставшиеся id в порядке первого
    появления.

    `allowed` — chunk id подтверждений, которые видела LLM. Так страница не может сослаться на
    чанк, которого модель не видела (выдуманный или из удалённого документа)."""
    cited: list[str] = []

    def replace(match: re.Match[str]) -> str:
        cid = match.group(1)
        if cid not in allowed:
            return ""
        if cid not in cited:
            cited.append(cid)
        return match.group(0)

    return _CITE.sub(replace, body), cited


def resolve_links(body: str, pages: dict[str, tuple[str, str, str]]) -> tuple[str, list[str]]:
    """Переписывает `[[Name|label]]` в `[[slug|label]]` для известных страниц, иначе в просто label.

    `pages` отображает normalize_name(name or alias) -> (entity_id, slug, title).
    Без `|label` подписью становится сам текст цели. Возвращает id связанных сущностей в
    порядке первого появления (из них `build` строит рёбра `LINKS_TO`).
    """
    linked: list[str] = []

    def replace(match: re.Match[str]) -> str:
        target = match.group(1).strip()
        label = (match.group(2) or target).strip()
        hit = pages.get(normalize_name(target))
        if hit is None:
            return label
        entity_id, slug, _title = hit
        if entity_id not in linked:
            linked.append(entity_id)
        return f"[[{slug}|{label}]]"

    return _LINK.sub(replace, body), linked
