from __future__ import annotations

import re

from mnogobase.ids import normalize_name

_CITE = re.compile(r"[ \t]?\[\^([^\]\s]+)\]")
# "#" разрешён в цели ссылки: имена сущностей вроде "C#" сохраняют его после normalize_name
_LINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
# только точные сгенерированные заголовки: секция от LLM вроде "## Related work" остаётся
_RESERVED = re.compile(r"^##[ \t]+(Related|Sources)[ \t]*$", re.IGNORECASE | re.MULTILINE)
_FOOTNOTE_DEF = re.compile(r"^\[\^[^\]]+\]:.*$", re.MULTILINE)


def strip_reserved_sections(body: str) -> str:
    """Убирает начальный заголовок `# `, сгенерированные секции Related/Sources и определения
    сносок."""
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
    появления."""
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
    Возвращает id связанных сущностей в порядке первого появления.
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
