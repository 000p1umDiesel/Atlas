"""Серверы моделей по `config.yaml`: `mnogobase models up | down | status | logs`.

Каждая роль (`embedder`, `rerank`, `llm`) с `serve: llamacpp | vllm` получает свой сервер на
порту роли. Команды серверов строятся здесь, из конфига; запускаются они либо в Docker
(`docker-compose.models.yml`, сервис `<роль>-<бэкенд>`, команда приходит переменной
`MB_<СЕРВИС>_ARGS`), либо нативно (llama.cpp на Mac: в Docker на Mac нет GPU)."""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

from mnogobase.config import ServedModel, Settings
from mnogobase.embedding.http import server_root

Runner = Literal["auto", "docker", "native"]
SERVICE_PREFIX = {"embedder": "embed", "rerank": "rerank", "llm": "llm"}
BACKENDS = ("llamacpp", "vllm")
CONTAINER_PORT = {"llamacpp": 8080, "vllm": 8000}
COMPOSE_FILE = "docker-compose.models.yml"
COMPOSE_PROJECT = "mnogobase-models"
# Qwen3-Reranker в vLLM: классификатор yes/no поверх LM и шаблон промпта из образа
QWEN3_RERANKER_OVERRIDES = {
    "architectures": ["Qwen3ForSequenceClassification"],
    "classifier_from_token": ["no", "yes"],
    "is_original_qwen3_reranker": True,
}
QWEN3_RERANKER_TEMPLATE = "/vllm-workspace/examples/pooling/score/template/qwen3_reranker.jinja"


class ServeError(RuntimeError):
    """Сервер модели нельзя запустить так, как просит конфиг."""


@dataclass(frozen=True)
class Served:
    """Роль, сервер которой поднимает `mnogobase models up`."""

    role: str
    settings: ServedModel

    @property
    def backend(self) -> str:
        return self.settings.serve

    @property
    def service(self) -> str:
        return f"{SERVICE_PREFIX[self.role]}-{self.backend}"

    @property
    def port(self) -> int:
        return self.settings.port

    @property
    def health_url(self) -> str:
        return f"{server_root(self.settings.url)}/health"


def served_roles(settings: Settings, roles: Sequence[str] = ()) -> list[Served]:
    """Роли с локальным сервером (`serve` не `external`), реранкер — только при
    `rerank.enabled`; `roles` сужает список."""
    unknown = set(roles) - set(SERVICE_PREFIX)
    if unknown:
        raise ServeError(f"unknown role {', '.join(sorted(unknown))}: use embedder, rerank, llm")
    candidates = [("embedder", settings.embedder), ("llm", settings.llm)]
    if settings.rerank.enabled:
        candidates.insert(1, ("rerank", settings.rerank))
    return [
        Served(role, s)
        for role, s in candidates
        if s.serve != "external" and (not roles or role in roles)
    ]


def server_args(served: Served, port: int) -> list[str]:
    """Аргументы сервера (без самой программы): `llama-server ...` или `vllm serve ...`."""
    s, c = served.settings, served.settings.server
    if served.backend == "llamacpp":
        # имя модели на сервере — `model`, как у vLLM, поэтому конфиг не зависит от бэкенда
        args = ["-hf", s.gguf or "", "--alias", s.model, "--port", str(port)]
        args += ["--ctx-size", str(c.ctx), "--n-gpu-layers", "999"]
        if served.role == "embedder":
            args += ["--embedding", "--ubatch-size", str(c.ctx)]
        elif served.role == "rerank":
            args += ["--rerank", "--ubatch-size", str(c.ctx)]
    else:
        args = [s.model, "--port", str(port), "--max-model-len", str(c.ctx)]
        args += ["--gpu-memory-utilization", str(c.gpu_memory)]
        if served.role in ("embedder", "rerank"):
            args += ["--runner", "pooling"]
        if served.role == "rerank" and "qwen3-reranker" in s.model.lower():
            args += ["--hf-overrides", json.dumps(QWEN3_RERANKER_OVERRIDES)]
            args += ["--chat-template", QWEN3_RERANKER_TEMPLATE]
    return args + c.args


def resolve_runner(runner: Runner) -> Literal["docker", "native"]:
    if runner != "auto":
        return runner
    return "native" if sys.platform == "darwin" else "docker"


def is_ready(served: Served, client: httpx.Client | None = None) -> bool:
    try:
        response = (client or httpx).get(served.health_url, timeout=2)
    except httpx.TransportError:
        return False
    return response.status_code == 200


class DockerRunner:
    """Сервисы `docker-compose.models.yml` в отдельном проекте compose: не мешают Qdrant и
    Neo4j из `docker-compose.yml` и не подхватывают `COMPOSE_FILE` / `COMPOSE_PROFILES`."""

    def __init__(
        self, compose_file: Path, run: Callable[..., subprocess.CompletedProcess] = subprocess.run
    ):
        if not compose_file.exists():
            raise ServeError(f"{compose_file} not found: run mnogobase from the repository root")
        self._file = compose_file
        self._run = run

    def _compose(self, *args: str, env: dict[str, str] | None = None, check: bool = True):
        profiles = [
            arg
            for prefix in SERVICE_PREFIX.values()
            for backend in BACKENDS
            for arg in ("--profile", f"{prefix}-{backend}")
        ]
        cmd = ["docker", "compose", "-p", COMPOSE_PROJECT, "-f", str(self._file), *profiles, *args]
        base = {
            k: v for k, v in os.environ.items() if k not in ("COMPOSE_FILE", "COMPOSE_PROFILES")
        }
        return self._run(cmd, env={**base, **(env or {})}, check=check)

    @staticmethod
    def _env(served: Sequence[Served]) -> dict[str, str]:
        return {
            f"MB_{s.service.upper().replace('-', '_')}_ARGS": shlex.join(
                server_args(s, CONTAINER_PORT[s.backend])
            )
            for s in served
        }

    def prune(self, served: Sequence[Served]) -> None:
        """Удаляет контейнеры ролей, которые сменили бэкенд или стали external: они занимают
        порт роли."""
        wanted = {s.service for s in served}
        stale = [
            f"{prefix}-{backend}"
            for prefix in SERVICE_PREFIX.values()
            for backend in BACKENDS
            if f"{prefix}-{backend}" not in wanted
        ]
        self._compose("rm", "--stop", "--force", *stale)

    def start(self, served: Sequence[Served]) -> None:
        # compose пересоздаёт контейнер, если команда (модель, флаги) изменилась
        self._compose("up", "-d", *(s.service for s in served), env=self._env(served))

    def alive(self, served: Served) -> bool:
        result = self._run(
            ["docker", "compose", "-p", COMPOSE_PROJECT, "-f", str(self._file),
             "--profile", served.service, "ps", "--status", "running", "-q", served.service],
            capture_output=True, text=True, check=False,
        )  # fmt: skip
        return bool(result.stdout.strip())

    def stop(self) -> None:
        self._compose("down")

    def logs(self, served: Served, follow: bool) -> None:
        self._compose(
            "logs", "--tail", "200", *(["-f"] if follow else []), served.service, check=False
        )


class NativeRunner:
    """llama-server как фоновые процессы: pid и аргументы в `<data_dir>/models/<роль>.json`,
    вывод в `<logs_dir>/models-<роль>.log`. vLLM нативно не запускается — только в Docker."""

    def __init__(
        self,
        state_dir: Path,
        logs_dir: Path,
        which: Callable[[str], str | None] | None = None,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    ):
        self._state = state_dir
        self._logs = logs_dir
        self._which = which or shutil.which
        self._popen = popen

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
        for s in served:
            if s.backend == "vllm":
                raise ServeError(
                    f"{s.role}: vLLM runs only in Docker on Linux with an NVIDIA GPU "
                    "(`mnogobase models up --runner docker`); here use serve: llamacpp"
                )
        binary = self._which("llama-server")
        if served and binary is None:
            raise ServeError("llama-server not found: brew install llama.cpp")
        self._state.mkdir(parents=True, exist_ok=True)
        self._logs.mkdir(parents=True, exist_ok=True)
        for s in served:
            args = [binary, *server_args(s, s.port)]
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
        """Останавливает серверы ролей, которые стали external или отключены."""
        wanted = {s.role for s in served}
        for role in SERVICE_PREFIX:
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
        for role in SERVICE_PREFIX:
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


ModelRunner = DockerRunner | NativeRunner


def make_runner(runner: Runner, settings: Settings) -> ModelRunner:
    if resolve_runner(runner) == "docker":
        return DockerRunner(Path(COMPOSE_FILE))
    return NativeRunner(settings.data_dir / "models", settings.logs_dir)


def wait_ready(
    served: Sequence[Served],
    runner: DockerRunner | NativeRunner,
    timeout: float,
    on_wait: Callable[[Served, float], None] = lambda s, elapsed: None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    ready: Callable[[Served], bool] = is_ready,
) -> None:
    """Ждёт `/health` каждого сервера: первая загрузка модели с Hugging Face идёт минуты."""
    start = clock()
    pending = list(served)
    while pending:
        pending = [s for s in pending if not ready(s)]
        if not pending:
            return
        for s in pending:
            if not runner.alive(s):
                raise ServeError(
                    f"{s.role}: the {s.backend} server exited, see `mnogobase models logs {s.role}`"
                )
        elapsed = clock() - start
        if elapsed > timeout:
            names = ", ".join(s.role for s in pending)
            raise ServeError(
                f"{names} not ready after {int(timeout)} s: see `mnogobase models logs`"
            )
        on_wait(pending[0], elapsed)
        sleep(5)


def bring_up(
    served: Sequence[Served],
    runner: DockerRunner | NativeRunner,
    timeout: float,
    on_wait: Callable[[Served, float], None] = lambda s, elapsed: None,
    **wait_kwargs,
) -> None:
    """Запускает серверы ролей и ждёт их готовности. llama.cpp стартуют вместе, vLLM — по
    одному: каждый при старте резервирует долю свободной видеопамяти, и параллельный старт
    сбивает этот расчёт."""
    runner.prune(served)
    llama = [s for s in served if s.backend == "llamacpp"]
    if llama:
        runner.start(llama)
    for s in served:
        if s.backend == "vllm":
            runner.start([s])
            wait_ready([s], runner, timeout, on_wait, **wait_kwargs)
    wait_ready(llama, runner, timeout, on_wait, **wait_kwargs)
