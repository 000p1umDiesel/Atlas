"""Серверы моделей для разработки: `mnogobase models up | down | status | logs`.

Каждая роль (`embedder`, `rerank`, `vision`, `llm`) с `serve: llamacpp` получает нативный
`llama-server` на порту роли (8001 / 8002 / 8003 / 8000). В развёртывании те же серверы —
сервисы `compose.yaml` с явными командами, и этот модуль там не нужен.

Вызывает CLI `cli/models.py`. Процессы запускаются в фоне в отдельной сессии и переживают
CLI; их pid и аргументы лежат в `<data_dir>/models/<роль>.json`, вывод — в
`<logs_dir>/models-<роль>.log`. Повторный `models up` с теми же аргументами ничего не
перезапускает, с другими — перезапускает сервер роли. Модели `llama-server` скачивает сам
(`-hf`) в свой кэш Hugging Face; с `device: cpu` слои на GPU не выгружаются.
"""

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

from mnogobase.core.config import ServedModel, Settings
from mnogobase.core.http import server_root

# все роли, которые можно поднять локально; порядок — порядок запуска и остановки
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
        """Фиксированный порт роли (8000–8003, `ServedModel.port`); `base_url` на него не
        влияет, поэтому локальный сервер должен слушать именно его."""
        return self.settings.port

    @property
    def health_url(self) -> str:
        """`/health` llama-server: в корне сервера, а не под `/v1`."""
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
    """Аргументы `llama-server` (без самой программы).

    Веса — `weights` (`gguf` или `model`) через `-hf`, имя модели на сервере — `--alias
    model`, контекст — `server.ctx`. `gpu=False` (`device: cpu`) не выгружает слои на GPU.
    embedder и reranker получают свой режим и `--ubatch-size` = контексту (весь вход
    обрабатывается одним микробатчем), в конец добавляются `server.args` из конфига. Должно
    совпадать с командами сервисов в `compose.yaml`."""
    s, c = served.settings, served.settings.server
    # имя модели на сервере — `model`; у VLM -hf сам подключает проектор картинок (mmproj)
    args = ["-hf", s.weights, "--alias", s.model, "--port", str(port), "--ctx-size", str(c.ctx)]
    # 999 = все слои на GPU (Metal на Mac), 0 = только CPU
    args += ["--n-gpu-layers", "999" if gpu else "0"]
    if served.role == "embedder":
        args += ["--embedding", "--ubatch-size", str(c.ctx)]
    elif served.role == "rerank":
        args += ["--rerank", "--ubatch-size", str(c.ctx)]
    return args + c.args


def is_ready(served: Served, client: httpx.Client | None = None) -> bool:
    """Отвечает ли сервер роли 200 на `/health` (модель загружена)."""
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
    вывод в `<logs_dir>/models-<роль>.log`.

    `which` и `popen` подменяются в тестах. Запись роли считается действующей, только если
    процесс с её pid жив; иначе она игнорируется, как будто сервера нет."""

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
        """Файл состояния роли: JSON `{pid, args}`."""
        return self._state / f"{role}.json"

    def log_path(self, role: str) -> Path:
        """Лог сервера роли (дописывается при каждом запуске)."""
        return self._logs / f"models-{role}.log"

    def _record(self, role: str) -> dict | None:
        """Запись роли, если файл читается и процесс жив; иначе None.

        pid может быть переиспользован ОС после смерти сервера; тогда запись ошибочно
        считается живой, и `_kill` пошлёт SIGTERM чужому процессу (риск принят: только
        разработка)."""
        try:
            record = json.loads(self._pidfile(role).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return record if _pid_alive(record.get("pid")) else None

    def start(self, served: Sequence[Served]) -> None:
        """Запускает серверы ролей, не дожидаясь готовности.

        Сервер с теми же аргументами, который уже работает, не трогается; с другими
        аргументами — останавливается и запускается заново. Процесс стартует в новой сессии
        (не получает Ctrl+C терминала и переживает CLI). Нет `llama-server` в PATH —
        `ServeError`."""
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
        """Жив ли процесс сервера роли (по pid из файла состояния)."""
        return self._record(served.role) is not None

    def _kill(self, role: str) -> None:
        """Шлёт SIGTERM серверу роли (если жив) и удаляет файл состояния; не ждёт выхода."""
        record = self._record(role)
        if record:
            with contextlib.suppress(ProcessLookupError):
                os.kill(record["pid"], signal.SIGTERM)
        self._pidfile(role).unlink(missing_ok=True)

    def stop(self) -> None:
        """Останавливает серверы всех ролей (`models down`)."""
        for role in ROLES:
            self._kill(role)

    def logs(self, served: Served, follow: bool) -> None:
        """Показывает последние 200 строк лога роли через `tail` (с `follow` — `tail -f`).
        Лога ещё нет — `ServeError`."""
        path = self.log_path(served.role)
        if not path.exists():
            raise ServeError(f"no log yet: {path}")
        subprocess.run(["tail", "-n", "200", *(["-f"] if follow else []), str(path)], check=False)


def _pid_alive(pid: object) -> bool:
    """Есть ли процесс с таким pid (сигнал 0 ничего не делает, только проверяет)."""
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
    """`NativeRunner` с путями из настроек; GPU выключен при `device: cpu`."""
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
    `on_wait` получает ещё не готовые серверы после каждой проверки.

    Опрос раз в 5 секунд. Процесс сервера умер — `ServeError` сразу (смотреть лог роли);
    не готов за `timeout` секунд — `ServeError`. `sleep`, `clock` и `ready` подменяются в
    тестах."""
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
    """Поднимает серверы по одному: запуск, затем ожидание готовности, затем следующий.

    Сначала останавливаются серверы ролей, которых нет в `keep` (по умолчанию — в
    `served`): роль стала `serve: api` или отключена. Прочие роли из `keep` не
    останавливаются, так что `models up embedder` не гасит работающий reranker. По одному —
    чтобы несколько первых загрузок моделей не делили память и сеть одновременно."""
    runner.prune(served if keep is None else keep)
    for s in served:
        runner.start([s])
        wait_ready([s], runner, timeout, on_wait, **wait_kwargs)
