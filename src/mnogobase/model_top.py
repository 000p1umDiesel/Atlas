"""`mnogobase models top`: нагрузка серверов моделей во время работы, как `htop` для ролей.

- llama.cpp: занятые слоты из `/slots`, счётчики токенов и очередь из `/metrics` (`--metrics`
  ставит `models up`);
- vLLM: запросы в работе и в очереди, счётчики токенов из `/metrics`;
- роль `api` (облачный LLM): вызовы `llm_call` за последнюю минуту из `logs/mnogobase.jsonl`;
- видеокарты — `nvidia-smi`, память нативных серверов — `ps`."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from mnogobase.config import ServedModel, Settings
from mnogobase.embedding.http import auth_headers, server_root
from mnogobase.log_view import parse_line

API_WINDOW = 60.0  # секунд журнала для нагрузки роли api
_LOG_TAIL_BYTES = 512_000  # сколько байт с конца журнала читать за опрос
# счётчики Prometheus: (llama.cpp, vLLM)
_TOKENS_IN = ("llamacpp:prompt_tokens_total", "vllm:prompt_tokens_total")
_TOKENS_OUT = ("llamacpp:tokens_predicted_total", "vllm:generation_tokens_total")
_WAITING = ("llamacpp:requests_deferred", "vllm:num_requests_waiting")
_RUNNING = ("llamacpp:requests_processing", "vllm:num_requests_running")


@dataclass
class RoleLoad:
    role: str
    settings: ServedModel
    state: str  # ready | down | api
    busy: int | None = None  # занятые слоты / запросы в работе
    slots: int | None = None
    waiting: int | None = None
    tok_in: float | None = None  # токенов/с
    tok_out: float | None = None
    total_in: int | None = None
    total_out: int | None = None
    memory: int | None = None  # байт RSS (нативный сервер)
    note: str = ""


@dataclass(frozen=True)
class Gpu:
    index: int
    name: str
    util: int  # %
    used: int  # МиБ
    total: int


@dataclass
class _Counters:
    at: float
    tokens_in: float
    tokens_out: float


def parse_metrics(text: str) -> dict[str, float]:
    """Prometheus text format: сумма значений метрики по всем меткам."""
    values: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name_part, _, value = line.rpartition(" ")
        name = name_part.split("{", 1)[0].strip()
        try:
            values[name] = values.get(name, 0.0) + float(value)
        except ValueError:
            continue
    return values


def _first(metrics: dict[str, float], names: Sequence[str]) -> float | None:
    return next((metrics[n] for n in names if n in metrics), None)


def gpus(run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> list[Gpu]:
    if shutil.which("nvidia-smi") is None:
        return []
    result = run(
        ["nvidia-smi", "--query-gpu=index,name,utilization.gpu,memory.used,memory.total",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    found = []
    for line in (result.stdout or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 5 and parts[0].isdigit():
            try:
                found.append(Gpu(int(parts[0]), parts[1], *(int(float(p)) for p in parts[2:])))
            except ValueError:
                continue
    return found


def rss(pid: int, run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> int | None:
    result = run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, check=False)
    value = (result.stdout or "").strip()
    return int(value) * 1024 if value.isdigit() else None


def api_load(
    log_path: Path, now: datetime, window: float = API_WINDOW
) -> tuple[int, int, int, float]:
    """`llm_call` за последние `window` секунд: (вызовы, токены in, токены out, средняя
    задержка, с). Читается только хвост журнала."""
    try:
        with log_path.open("rb") as file:
            file.seek(0, os.SEEK_END)
            file.seek(max(0, file.tell() - _LOG_TAIL_BYTES))
            lines = file.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return 0, 0, 0, 0.0
    calls = tokens_in = tokens_out = 0
    duration = 0.0
    for line in lines:
        record = parse_line(line)
        if not record or record.get("event") != "llm_call":
            continue
        try:
            ts = datetime.fromisoformat(str(record.get("ts")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if (now - ts).total_seconds() > window:
            continue
        calls += 1
        tokens_in += int(record.get("tokens_in") or 0)
        tokens_out += int(record.get("tokens_out") or 0)
        duration += float(record.get("duration_ms") or 0) / 1000
    return calls, tokens_in, tokens_out, duration / calls if calls else 0.0


@dataclass
class ModelTop:
    """Снимки нагрузки ролей; `poll()` раз в несколько секунд, скорости — по разнице
    счётчиков между опросами."""

    settings: Settings
    pid_of: Callable[[str], int | None] = lambda role: None
    client: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=1.5))
    clock: Callable[[], float] = time.monotonic
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    gpu_query: Callable[[], list[Gpu]] = gpus
    memory_of: Callable[[int], int | None] = rss
    _seen: dict[str, _Counters] = field(default_factory=dict)

    def roles(self) -> list[tuple[str, ServedModel]]:
        s = self.settings
        roles: list[tuple[str, ServedModel]] = [("embedder", s.embedder)]
        if s.rerank.enabled:
            roles.append(("rerank", s.rerank))
        if s.vision.enabled:
            roles.append(("vision", s.vision))
        return [*roles, ("llm", s.llm)]

    def poll(self) -> tuple[list[RoleLoad], list[Gpu]]:
        loads = [self._load(role, s) for role, s in self.roles()]
        return loads, self.gpu_query()

    def _load(self, role: str, s: ServedModel) -> RoleLoad:
        if s.serve == "api":
            return self._api(role, s)
        load = RoleLoad(role, s, "down")
        root = server_root(s.url)
        try:
            metrics = parse_metrics(self._get(f"{root}/metrics", s).text)
        except httpx.HTTPError:
            self._seen.pop(role, None)
            return load
        load.state = "ready"
        if s.serve == "llamacpp":
            try:
                slots = self._get(f"{root}/slots", s).json()
                load.slots = len(slots)
                load.busy = sum(1 for slot in slots if slot.get("is_processing"))
            except (httpx.HTTPError, ValueError):
                pass
        else:
            running = _first(metrics, _RUNNING)
            load.busy = int(running) if running is not None else None
        waiting = _first(metrics, _WAITING)
        load.waiting = int(waiting) if waiting is not None else None
        tokens_in = _first(metrics, _TOKENS_IN) or 0.0
        tokens_out = _first(metrics, _TOKENS_OUT) or 0.0
        load.total_in, load.total_out = int(tokens_in), int(tokens_out)
        now = self.clock()
        before = self._seen.get(role)
        if before is not None and now > before.at and tokens_in >= before.tokens_in:
            elapsed = now - before.at
            load.tok_in = (tokens_in - before.tokens_in) / elapsed
            load.tok_out = (tokens_out - before.tokens_out) / elapsed
        self._seen[role] = _Counters(now, tokens_in, tokens_out)
        pid = self.pid_of(role)
        if pid is not None:
            load.memory = self.memory_of(pid)
        return load

    def _api(self, role: str, s: ServedModel) -> RoleLoad:
        load = RoleLoad(role, s, "api")
        if role != "llm":
            return load
        calls, tokens_in, tokens_out, latency = api_load(
            self.settings.logs_dir / "mnogobase.jsonl", self.now()
        )
        load.tok_in, load.tok_out = tokens_in / API_WINDOW, tokens_out / API_WINDOW
        load.note = f"{calls} calls/min" + (f", avg {latency:.1f} s" if calls else "")
        return load

    def _get(self, url: str, s: ServedModel) -> httpx.Response:
        response = self.client.get(url, headers=auth_headers(s.api_key_env))
        response.raise_for_status()
        return response
