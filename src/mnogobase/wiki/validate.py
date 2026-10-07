from __future__ import annotations

import re

from mnogobase.ids import normalize_name

_CITE = re.compile(r"[ \t]?\[\^([^\]\s]+)\]")
# "#" is allowed in the target: entity names such as "C#" keep it after normalize_name
_LINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
# only the exact generated headings: an LLM section such as "## Related work" is kept
_RESERVED = re.compile(r"^##[ \t]+(Related|Sources)[ \t]*$", re.IGNORECASE | re.MULTILINE)
_FOOTNOTE_DEF = re.compile(r"^\[\^[^\]]+\]:.*$", re.MULTILINE)


def strip_reserved_sections(body: str) -> str:
    """Drop a leading `# ` title, the generated Related/Sources sections and footnote definitions."""
    body = body.strip()
    if body.startswith("# "):
        body = body.split("\n", 1)[1] if "\n" in body else ""
    match = _RESERVED.search(body)
    if match:
        body = body[: match.start()]
    body = _FOOTNOTE_DEF.sub("", body)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def cited_ids(body: str) -> list[str]:
    """The `[^id]` citation ids of a page body, de-duplicated in first-seen order."""
    return list(dict.fromkeys(m.group(1) for m in _CITE.finditer(body)))


def validate_citations(body: str, allowed: set[str]) -> tuple[str, list[str]]:
    """Remove `[^id]` refs whose id is not allowed; return the kept ids in first-seen order."""
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
    """Rewrite `[[Name|label]]` to `[[slug|label]]` for known pages, plain label otherwise.

    `pages` maps normalize_name(name or alias) -> (entity_id, slug, title).
    Returns the linked entity ids in first-seen order.
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
