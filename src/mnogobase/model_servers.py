"""Серверы моделей для разработки: `mnogobase models up | down | status | logs`.

Каждая роль (`embedder`, `rerank`, `vision`, `llm`) с `serve: llamacpp` получает нативный
`llama-server` на порту роли (8001 / 8002 / 8003 / 8000). В развёртывании те же серверы —
сервисы `compose.yaml` с явными командами, и этот модуль там не нужен."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

from mnogobase.config import ServedModel, Settings
from mnogobase.http import server_root

ROLES = ("embedder", "rerank", "vision", "llm")


class ServeError(RuntimeError):
    """Сервер модели нельзя запустить так, как просит конфиг."""


@dataclass(frozen=True)
class Served:
    """Роль, сервер которой поднимает `mnogobase models up`."""

    role: str
    settings: ServedModel

    @property
    def port(self) -> int:
        return self.settings.port

    @property
    def health_url(self) -> str:
        return f"{server_root(self.settings.url)}/health"


def served_roles(settings: Settings, roles: Sequence[str] = ()) -> list[Served]:
    """Роли с локальным сервером (`serve: llamacpp`), реранкер и VLM — только при
    `enabled`; `roles` сужает список."""
    unknown = set(roles) - set(ROLES)
    if unknown:
        raise ServeError(f"unknown role {', '.join(sorted(unknown))}: use {', '.join(ROLES)}")
    candidates: list[tuple[str, ServedModel]] = [("embedder", settings.embedder)]
    if settings.rerank.enabled:
        candidates.append(("rerank", settings.rerank))
    if settings.vision.enabled:
        candidates.append(("vision", settings.vision))
    candidates.append(("llm", settings.llm))
    return [
        Served(role, s)
        for role, s in candidates
        if s.serve == "llamacpp" and (not roles or role in roles)
    ]


def server_args(served: Served, port: int, *, gpu: bool = True) -> list[str]:
    """Аргументы `llama-server` (без самой программы)."""
    s, c = served.settings, served.settings.server
    # имя модели на сервере — `model`; у VLM -hf сам подключает проектор картинок (mmproj)
    args = ["-hf", s.weights, "--alias", s.model, "--port", str(port), "--ctx-size", str(c.ctx)]
    args += ["--n-gpu-layers", "999" if gpu else "0"]  # Metal на Mac
    if served.role == "embedder":
        args += ["--embedding", "--ubatch-size", str(c.ctx)]
    elif served.role == "rerank":
        args += ["--rerank", "--ubatch-size", str(c.ctx)]
    return args + c.args


def is_ready(served: Served, client: httpx.Client | None = None) -> bool:
    return health_state(served, client) == "ready"


def health_state(served: Served, client: httpx.Client | None = None) -> str:
    """Что сервер роли сообщает на `/health`, словами для `models up`."""
    try:
        response = (client or httpx).get(served.health_url, timeout=2)
    except httpx.TransportError:
        return "starting or downloading the model"
    if response.status_code == 200:
        return "ready"
    if response.status_code == 503:  # модель ещё грузится в память
        return "loading the model"
    return f"HTTP {response.status_code}"


class NativeRunner:
    """llama-server как фоновые процессы: pid и аргументы в `<data_dir>/models/<роль>.json`,
    вывод в `<logs_dir>/models-<роль>.log`."""

    def __init__(
        self,
        state_dir: Path,
        logs_dir: Path,
        which: Callable[[str], str | None] | None = None,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        *,
        gpu: bool = True,
    ):
        self._state = state_dir
        self._logs = logs_dir
        self._which = which or shutil.which
        self._popen = popen
        self.gpu = gpu

    def _pidfile(self, role: str) -> Path:
        return self._state / f"{role}.json"

    def log_path(self, role: str) -> Path:
        return self._logs / f"models-{role}.log"

    def _record(self, role: str) -> dict | None:
        try:
            record = json.loads(self._pidfile(role).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return record if _pid_alive(record.get("pid")) else None

    def start(self, served: Sequence[Served]) -> None:
        binary = self._which("llama-server")
        if served and binary is None:
            raise ServeError("llama-server not found: brew install llama.cpp")
        self._state.mkdir(parents=True, exist_ok=True)
        self._logs.mkdir(parents=True, exist_ok=True)
        for s in served:
            args = [binary, *server_args(s, s.port, gpu=self.gpu)]
            record = self._record(s.role)
            if record and record.get("args") == args:
                continue
            self._kill(s.role)  # другая модель или флаги: перезапуск
            with self.log_path(s.role).open("ab") as log:
                proc = self._popen(
                    args, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
                )
            self._pidfile(s.role).write_text(
                json.dumps({"pid": proc.pid, "args": args}), encoding="utf-8"
            )

    def prune(self, served: Sequence[Served]) -> None:
        """Останавливает серверы ролей, которые стали api или отключены."""
        wanted = {s.role for s in served}
        for role in ROLES:
            if role not in wanted:
                self._kill(role)

    def alive(self, served: Served) -> bool:
        return self._record(served.role) is not None

    def _kill(self, role: str) -> None:
        record = self._record(role)
        if record:
            with contextlib.suppress(ProcessLookupError):
                os.kill(record["pid"], signal.SIGTERM)
        self._pidfile(role).unlink(missing_ok=True)

    def stop(self) -> None:
        for role in ROLES:
            self._kill(role)

    def logs(self, served: Served, follow: bool) -> None:
        path = self.log_path(served.role)
        if not path.exists():
            raise ServeError(f"no log yet: {path}")
        subprocess.run(["tail", "-n", "200", *(["-f"] if follow else []), str(path)], check=False)


def _pid_alive(pid: object) -> bool:
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # процесс есть, но чужой
        return True
    return True


def make_runner(settings: Settings) -> NativeRunner:
    return NativeRunner(
        settings.data_dir / "models", settings.logs_dir, gpu=settings.device != "cpu"
    )


def wait_ready(
    served: Sequence[Served],
    runner: NativeRunner,
    timeout: float,
    on_wait: Callable[[Sequence[Served], float], None] = lambda pending, elapsed: None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    ready: Callable[[Served], bool] = is_ready,
) -> None:
    """Ждёт `/health` каждого сервера: первая загрузка модели с Hugging Face идёт минуты.
    `on_wait` получает ещё не готовые серверы после каждой проверки."""
    start = clock()
    pending = list(served)
    while pending:
        pending = [s for s in pending if not ready(s)]
        if not pending:
            return
        for s in pending:
            if not runner.alive(s):
                raise ServeError(
                    f"{s.role}: llama-server exited, see `mnogobase models logs {s.role}`"
                )
        elapsed = clock() - start
        if elapsed > timeout:
            names = ", ".join(s.role for s in pending)
            raise ServeError(
                f"{names} not ready after {int(timeout)} s: see `mnogobase models logs`"
            )
        on_wait(pending, elapsed)
        sleep(5)


def bring_up(
    served: Sequence[Served],
    runner: NativeRunner,
    timeout: float,
    on_wait: Callable[[Sequence[Served], float], None] = lambda pending, elapsed: None,
    keep: Sequence[Served] | None = None,
    **wait_kwargs,
) -> None:
    """Последовательный запуск; прочие роли из `keep` не останавливаются."""
    runner.prune(served if keep is None else keep)
    for s in served:
        runner.start([s])
        wait_ready([s], runner, timeout, on_wait, **wait_kwargs)
