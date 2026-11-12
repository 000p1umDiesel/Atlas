"""Выбор вычислительного устройства для локальных моделей Docling (layout, OCR).

`build_app` передаёт результат `detect_device(settings.device)` в `DoclingParser`, `doctor`
показывает его в проверках. Эмбеддер, реранкер, VLM и LLM работают на отдельных серверах и
устройство выбирают сами. Поддерживаются только macOS (MPS или CPU) и Linux (CPU).
"""

from __future__ import annotations

from typing import Literal

Device = Literal["mps", "cpu"]


def _mps_available() -> bool:
    """Доступен ли Metal (MPS) в установленном torch; на Linux всегда `False`."""
    import torch  # тяжёлый импорт: только когда устройство действительно выбирают

    return bool(torch.backends.mps.is_available())


def detect_device(preference: str = "auto") -> Device:
    """Устройство по настройке `device`: явное `mps` / `cpu` возвращается как есть, `auto`
    выбирает MPS, если он доступен, иначе CPU. Явный `mps` не проверяется — если его нет,
    ошибка всплывёт при загрузке модели в Docling."""
    if preference in ("mps", "cpu"):
        return preference  # type: ignore[return-value]
    return "mps" if _mps_available() else "cpu"
