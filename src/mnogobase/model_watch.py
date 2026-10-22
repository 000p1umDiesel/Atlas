"""Прогресс `mnogobase models up`: сколько весов роли скачано и что делает её сервер.

llama.cpp и vLLM качают веса молча, поэтому прогресс считается снаружи. Какие файлы нужны,
берётся из списка файлов репозитория на Hugging Face; сколько скачано — из размера этих файлов
в кэше сервера. Блоб в кэше Hugging Face называется по sha256 файла
(`blobs/<sha256>`, недокачанный — `<sha256>.incomplete` / `.downloadInProgress`), а в плоском
кэше llama.cpp — `<org>_<repo>_<файл>`."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal, Protocol

from mnogobase.serving import Served, is_ready

# queued: сервер ещё не запущен (vLLM стартуют по очереди); starting: неизвестно, сколько
# качать (нет списка файлов); downloading; loading: веса скачаны, сервер грузит их в память
Phase = Literal["queued", "starting", "downloading", "loading", "ready"]
# какой проектор картинок llama.cpp берёт из репозитория с несколькими mmproj
_MMPROJ_PREFERENCE = ("bf16", "f16", "f32")
_RATE_SMOOTHING = 0.3  # вес нового замера скорости в скользящем среднем


@dataclass(frozen=True)
class WeightFile:
    path: str
    sha256: str
    size: int

    @property
    def name(self) -> str:
        return PurePosixPath(self.path).name


@dataclass
class RoleStatus:
    served: Served
    phase: Phase
    done: int = 0  # байт весов в кэше
    total: int | None = None  # None: список файлов не получен (нет сети, закрытый репозиторий)
    rate: float | None = None  # байт/с
    cached: bool = False  # веса уже были в кэше до запуска

    @property
    def eta(self) -> float | None:
        if self.total is None or not self.rate:
            return None
        return max(self.total - self.done, 0) / self.rate


class CacheLister(Protocol):
    def alive(self, served: Served) -> bool: ...

    def cache_listing(self, served: Served) -> list[tuple[int, str]]:
        """`(размер, путь)` файлов в кэше моделей сервера роли."""
        ...


TreeLister = Callable[[str], Iterable[WeightFile]]


def hf_tree(repo: str) -> list[WeightFile]:
    """LFS-файлы репозитория (веса); токен и зеркало — из настроек huggingface_hub."""
    from huggingface_hub import HfApi

    return [
        WeightFile(f.path, f.lfs.sha256, f.size)
        for f in HfApi().list_repo_tree(repo, recursive=True)
        if getattr(f, "lfs", None) is not None
    ]


def weight_files(served: Served, tree: TreeLister = hf_tree) -> list[WeightFile]:
    """Файлы, которые скачает сервер роли: GGUF кванта (все части) и проектор картинок
    (`mmproj`, если он есть в репозитории) у llama.cpp; safetensors у vLLM."""
    s = served.settings
    if served.backend == "llamacpp":
        repo, _, quant = (s.gguf or "").partition(":")
        files = [f for f in tree(repo) if f.path.endswith(".gguf")]
        mmproj = [f for f in files if f.name.lower().startswith("mmproj")]
        weights = [f for f in files if f not in mmproj and _is_quant(f.path, quant)]
        if mmproj:
            weights.append(min(mmproj, key=_mmproj_rank))
        return weights
    files = list(tree(s.model))
    safetensors = [f for f in files if f.path.endswith(".safetensors")]
    return safetensors or [f for f in files if f.path.endswith(".bin")]


def _is_quant(path: str, quant: str) -> bool:
    """`Q4_K_M` — это `model-Q4_K_M.gguf`, `Q4_K_M/model-00001-of-00002.gguf`, но не
    `model-UD-Q4_K_M.gguf` (другая квантизация Unsloth)."""
    if not quant:
        return True
    q = re.escape(quant.lower())
    return re.search(rf"(^|[-_./])(?<!ud-){q}([-_./]|$)", path.lower()) is not None


def _mmproj_rank(f: WeightFile) -> tuple[int, str]:
    name = f.name.lower()
    for rank, precision in enumerate(_MMPROJ_PREFERENCE):
        if f"-{precision}." in name:
            return rank, name
    return len(_MMPROJ_PREFERENCE), name


def downloaded(files: Sequence[WeightFile], repo: str, listing: Iterable[tuple[int, str]]) -> int:
    """Сколько байт `files` лежит в кэше, включая недокачанные части."""
    flat_prefix = repo.replace("/", "_") + "_"
    best = dict.fromkeys(files, 0)
    for size, path in listing:
        base = PurePosixPath(path).name
        for f in files:
            if base.startswith(f.sha256) or (base.startswith(flat_prefix) and f.name in base):
                best[f] = max(best[f], min(size, f.size))
    return sum(best.values())


def _repo(served: Served) -> str:
    if served.backend == "llamacpp":
        return (served.settings.gguf or "").partition(":")[0]
    return served.settings.model


class ModelWatch:
    """Состояние серверов ролей для живой таблицы `models up`; `poll()` раз в несколько
    секунд. Список файлов спрашивается у Hugging Face один раз на роль."""

    def __init__(
        self,
        served: Sequence[Served],
        runner: CacheLister,
        *,
        tree: TreeLister = hf_tree,
        ready: Callable[[Served], bool] = is_ready,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._served = list(served)
        self._runner = runner
        self._tree = tree
        self._ready = ready
        self._clock = clock
        self._files: dict[str, list[WeightFile] | None] = {}
        self._status = {s.role: RoleStatus(s, "queued") for s in served}
        self._seen: dict[str, tuple[float, int]] = {}

    def _weights(self, served: Served) -> list[WeightFile] | None:
        if served.role not in self._files:
            try:
                self._files[served.role] = weight_files(served, self._tree) or None
            except Exception:  # без сети прогресс показывает только скачанные байты
                self._files[served.role] = None
        return self._files[served.role]

    def poll(self) -> list[RoleStatus]:
        for s in self._served:
            status = self._status[s.role]
            if self._ready(s):
                status.phase = "ready"
                status.rate = None
                continue
            if not self._runner.alive(s):
                status.phase = "queued"
                continue
            files = self._weights(s)
            if files is None:
                status.phase = "starting"
                continue
            fresh = status.phase == "queued"  # первый опрос запущенного сервера
            done = downloaded(files, _repo(s), self._runner.cache_listing(s))
            self._track_rate(status, done)
            status.done, status.total = done, sum(f.size for f in files)
            if done < status.total:
                status.phase = "downloading"
            else:
                status.phase, status.rate = "loading", None
                status.cached = status.cached or fresh
        return [self._status[s.role] for s in self._served]

    def _track_rate(self, status: RoleStatus, done: int) -> None:
        now = self._clock()
        role = status.served.role
        if role in self._seen:
            then, before = self._seen[role]
            if now > then and done >= before:
                rate = (done - before) / (now - then)
                status.rate = (
                    rate
                    if status.rate is None
                    else _RATE_SMOOTHING * rate + (1 - _RATE_SMOOTHING) * status.rate
                )
        self._seen[role] = (now, done)
