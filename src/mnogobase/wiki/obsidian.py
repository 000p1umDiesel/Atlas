"""Настройки графа Obsidian для папки wiki (`wiki.obsidian`).

Папку `wiki/` можно открыть в Obsidian как хранилище. `write_graph_settings` дописывает в
`.obsidian/graph.json` фильтр, скрывающий служебные `index.md` и `log.md` (иначе они —
центр графа, связанный со всеми страницами), и цветовые группы по полю `type` из front
matter страниц. Ручные настройки пользователя сохраняются.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from mnogobase.core.files import write_atomic

# `path:` ищет подстроку, поэтому якоря: `-file:log` скрыл бы и `entities/logistic-regression.md`
GRAPH_FILTER = r"-path:/^(index|log)\.md$/"
# цвет запасного типа `Other` — серый, чтобы не спорить с осмысленными типами
OTHER_COLOR = 0xBAB0AC
# хорошо различимые цвета (Tableau 20 без серых); типы берут их по порядку из entity_types
_PALETTE = (
    0x4E79A7,
    0xF28E2B,
    0xE15759,
    0x76B7B2,
    0x59A14F,
    0xEDC948,
    0xB07AA1,
    0xFF9DA7,
    0x9C755F,
    0xA0CBE8,
    0xFFBE7D,
    0x8CD17D,
    0xB6992D,
    0xF1CE63,
    0x86BCB6,
    0xD37295,
    0xFABFD2,
    0xD4A6C8,
    0x499894,
)


def _type_query(entity_type: str) -> str:
    """Поисковый запрос Obsidian по свойству `type`; значение с пробелами — в кавычках."""
    if any(c.isspace() for c in entity_type):
        return f'[type:"{entity_type}"]'
    return f"[type:{entity_type}]"


def write_graph_settings(wiki_dir: Path, entity_types: Sequence[str]) -> bool:
    """Дописывает в `.obsidian/graph.json` фильтр служебных страниц и цветовые группы по `type`.

    Добавляется только недостающее: свой фильтр, свои группы и перекрашенные типы пользователя
    остаются. Нечитаемый файл не трогается. Возвращает, был ли файл записан.

    Новый тип получает первый цвет палитры, ещё не занятый группами в файле; когда свободные
    цвета кончаются — цвет по номеру типа (по кругу). Файл перезаписывается атомарно и только
    если содержимое изменилось.
    """
    path = wiki_dir / ".obsidian" / "graph.json"
    try:
        graph = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return False
    if not isinstance(graph, dict):
        return False
    before = json.dumps(graph, sort_keys=True)
    search = str(graph.get("search") or "")
    if GRAPH_FILTER not in search:
        graph["search"] = f"{GRAPH_FILTER} {search}".strip()
    groups = graph.get("colorGroups")
    # чужие или повреждённые элементы (не словари) отбрасываются
    groups = [g for g in groups if isinstance(g, dict)] if isinstance(groups, list) else []
    queries = {g.get("query") for g in groups}
    used = {g["color"].get("rgb") for g in groups if isinstance(g.get("color"), dict)}
    free = (c for c in _PALETTE if c not in used)
    for i, entity_type in enumerate(entity_types):
        query = _type_query(entity_type)
        if query in queries:
            continue
        color = OTHER_COLOR if entity_type == "Other" else next(free, _PALETTE[i % len(_PALETTE)])
        groups.append({"query": query, "color": {"a": 1, "rgb": color}})
    graph["colorGroups"] = groups
    if json.dumps(graph, sort_keys=True) == before:
        return False
    write_atomic(path, json.dumps(graph, ensure_ascii=False, indent=2))
    return True
