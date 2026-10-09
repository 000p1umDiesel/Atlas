from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import groupby
from pathlib import Path
from typing import NamedTuple

import yaml

from mnogobase.models import EntityRecord, PageIndexRow, RelationView
from mnogobase.wiki.validate import strip_reserved_sections

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_CITE_MARK = re.compile(r"[ \t]?\[\^([^\]\s]+)\]")
_LINK_LABEL = re.compile(r"\[\[[^\]|]+\|([^\]]+)\]\]")


@dataclass
class SourceRef:
    chunk_id: str
    path: str | None
    page: int | None


class Section(NamedTuple):
    """Одна секция страницы для эмбеддинга: цитаты убраны из `text`, их id сохранены в
    `chunk_ids`."""

    name: str
    text: str
    chunk_ids: list[str]


def _link(entity_id: str, name: str, linked: dict[str, str]) -> str:
    slug = linked.get(entity_id)
    return f"[[{slug}|{name}]]" if slug else name


def render_page(
    entity: EntityRecord,
    body: str,
    relations: list[RelationView],
    linked: dict[str, str],
    sources: list[SourceRef],
    version: int,
    updated: date,
) -> str:
    """Рендерит полную wiki-страницу; `linked` отображает entity_id -> slug существующих страниц."""
    front = {
        "id": entity.entity_id,
        "type": entity.type,
        "aliases": entity.aliases,
        "sources": len({s.path for s in sources if s.path}),
        # остаётся строкой, чтобы parse_page читал её обратно без изменений
        "updated": updated.isoformat(),
        "version": version,
    }
    parts = [
        "---",
        yaml.safe_dump(front, allow_unicode=True, sort_keys=False).strip(),
        "---",
        "",
        f"# {entity.name}",
        "",
        body.strip(),
        "",
    ]
    if relations:
        parts.append("## Related")
        for r in relations:
            if r.src_id == entity.entity_id:
                parts.append(f"- {r.predicate} → {_link(r.dst_id, r.dst_name, linked)}")
            else:
                parts.append(f"- {r.predicate} ← {_link(r.src_id, r.src_name, linked)}")
        parts.append("")
    if sources:
        parts.append("## Sources")
        for s in sources:
            where = f"*{Path(s.path).name}*" if s.path else "*unknown source*"
            if s.page:
                where += f", p.{s.page}"
            parts.append(f"[^{s.chunk_id}]: {where}")
        parts.append("")
    return "\n".join(parts)


def parse_page(text: str) -> tuple[dict, str]:
    """Страница -> (front matter, написанное LLM тело без заголовка, Related, Sources)."""
    meta: dict = {}
    match = _FRONTMATTER.match(text)
    if match:
        meta = yaml.safe_load(match.group(1)) or {}
        text = text[match.end() :]
    return meta, strip_reserved_sections(text)


def split_sections(text: str) -> list[Section]:
    """Страница -> секции для эмбеддинга; Sources пропускается, пустые секции отбрасываются."""
    match = _FRONTMATTER.match(text)
    body = text[match.end() :] if match else text
    title = ""
    current = "Summary"
    buffer: list[str] = []
    raw: list[tuple[str, list[str]]] = []
    for line in body.splitlines():
        if line.startswith("# ") and not title:
            title = line[2:].strip()
            continue
        if line.startswith("## "):
            raw.append((current, buffer))
            current, buffer = line[3:].strip(), []
            continue
        buffer.append(line)
    raw.append((current, buffer))
    out: list[Section] = []
    for name, lines in raw:
        if name.casefold() == "sources":
            continue
        joined = "\n".join(lines)
        chunk_ids = list(dict.fromkeys(m.group(1) for m in _CITE_MARK.finditer(joined)))
        content = _LINK_LABEL.sub(r"\1", _CITE_MARK.sub("", joined)).strip()
        if content:
            out.append(
                Section(name, f"{title} — {name}\n{content}" if title else content, chunk_ids)
            )
    return out


def render_index(rows: list[PageIndexRow]) -> str:
    lines = ["# Wiki Index", "", f"_{len(rows)} pages, generated automatically._", ""]
    ordered = sorted(rows, key=lambda r: (r.entity_type, r.title.casefold()))
    for entity_type, group in groupby(ordered, key=lambda r: r.entity_type):
        lines.append(f"## {entity_type}")
        lines.extend(f"- [[{r.slug}|{r.title}]]" for r in group)
        lines.append("")
    return "\n".join(lines)


def append_log(
    path: Path,
    run_id: str,
    documents: list[str],
    created: list[str],
    updated: list[str],
    deleted: list[str],
    now: datetime | None = None,
) -> None:
    """Дописывает в wiki-лог одну запись о запуске (обработанные документы, изменения страниц)."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("# Wiki Log\n", encoding="utf-8")
    lines = ["", f"## {now:%Y-%m-%d %H:%M:%S} UTC · run {run_id or '-'}"]
    changes = (("created", created), ("updated", updated), ("deleted", deleted))
    for label, names in (("documents", documents), *changes):
        if names:
            lines.append(f"- {label} ({len(names)}): " + ", ".join(names))
    if len(lines) == 2:
        lines.append("- no changes")
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
