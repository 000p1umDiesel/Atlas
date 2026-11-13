"""Чистые функции оформления ответов API: без БД и ввода-вывода."""

from __future__ import annotations

from mnogobase.core.config import Settings


def type_catalog(settings: Settings, counts: dict[str, int]) -> list[dict]:
    """Каталог типов сущностей: `[{type, label, description, count}]`.

    Сначала типы из `extract.entity_types` в порядке конфига (счётчик 0, если типа нет
    в `counts`), затем типы из `counts`, которых нет в конфиге: по убыванию счётчика, потом
    по ключу. Подпись берётся из `web.entity_types`; без записи подпись равна ключу, а
    описание — английскому описанию из `extract.entity_types`."""
    labels = settings.web.entity_types
    configured = settings.extract.entity_types

    def row(name: str, fallback: str) -> dict:
        entry = labels.get(name)
        return {
            "type": name,
            "label": entry.label if entry else name,
            "description": entry.description if entry else fallback,
            "count": counts.get(name, 0),
        }

    rows = [row(name, text) for name, text in configured.items()]
    extra = sorted((n for n in counts if n not in configured), key=lambda n: (-counts[n], n))
    return rows + [row(name, "") for name in extra]
