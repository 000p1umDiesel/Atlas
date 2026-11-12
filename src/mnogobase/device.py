from __future__ import annotations

from typing import Literal

Device = Literal["mps", "cpu"]


def _mps_available() -> bool:
    import torch  # тяжёлый импорт: только когда устройство действительно выбирают

    return bool(torch.backends.mps.is_available())


def detect_device(preference: str = "auto") -> Device:
    if preference in ("mps", "cpu"):
        return preference  # type: ignore[return-value]
    return "mps" if _mps_available() else "cpu"
