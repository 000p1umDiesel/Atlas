from __future__ import annotations

import platform
import shutil
from typing import Literal

Device = Literal["cuda", "mps", "cpu"]


def _cuda_available() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available())
    except ImportError:
        return shutil.which("nvidia-smi") is not None


def _mps_available() -> bool:
    try:
        import torch

        return bool(torch.backends.mps.is_available())
    except ImportError:
        return platform.system() == "Darwin" and platform.machine() == "arm64"


def detect_device(preference: str = "auto") -> Device:
    if preference in ("cuda", "mps", "cpu"):
        return preference  # type: ignore[return-value]
    if _cuda_available():
        return "cuda"
    if _mps_available():
        return "mps"
    return "cpu"
