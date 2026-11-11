import json
import subprocess
import sys

import httpx
import pytest

from mnogobase.config import (
    EmbedderSettings,
    LLMSettings,
    RerankSettings,
    ServerSettings,
    Settings,
    VisionSettings,
)
from mnogobase.model_servers import (
    NativeRunner,
    Served,
    ServeError,
    bring_up,
    health_state,
    is_ready,
    served_roles,
    server_args,
    wait_ready,
)

LOCAL_LLM = LLMSettings(
    model="Qwen/Qwen3-8B", serve="llamacpp", gguf="Qwen/Qwen3-8B-GGUF:Q4_K_M", base_url=None
)


def settings(**update) -> Settings:
    return Settings().model_copy(update=update)


def test_served_roles_skip_api_and_disabled():
    assert [s.role for s in served_roles(settings())] == ["embedder"]  # llm api, rerank off
    full = settings(rerank=RerankSettings(enabled=True), llm=LOCAL_LLM)
    assert [s.role for s in served_roles(full)] == ["embedder", "rerank", "llm"]
    assert [s.role for s in served_roles(full, ["llm"])] == ["llm"]
    with_vision = settings(vision=VisionSettings(enabled=True), llm=LOCAL_LLM)
    assert [s.role for s in served_roles(with_vision)] == ["embedder", "vision", "llm"]
    with pytest.raises(ServeError, match="unknown role nope"):
        served_roles(full, ["nope"])


def test_llamacpp_args_name_the_model():
    embed = Served("embedder", EmbedderSettings(server=ServerSettings(ctx=4096, args=["-np", "8"])))
    assert server_args(embed, 8080) == [
        "-hf", "Qwen/Qwen3-Embedding-4B-GGUF:Q4_K_M", "--alias", "Qwen/Qwen3-Embedding-4B-GGUF:Q4_K_M",
        "--port", "8080", "--ctx-size", "4096", "--n-gpu-layers", "999",
        "--embedding", "--ubatch-size", "4096", "-np", "8",
    ]  # fmt: skip
    assert "--n-gpu-layers" in (cpu := server_args(embed, 8080, gpu=False))
    assert cpu[cpu.index("--n-gpu-layers") + 1] == "0"
    rerank = server_args(Served("rerank", RerankSettings()), 8002)
    assert "--rerank" in rerank and "gorse-io/Qwen3-Reranker-4B-GGUF:Q4_K_M" in rerank
    # отдельный gguf: запускается он, а model остаётся именем на сервере
    llm = server_args(Served("llm", LOCAL_LLM), 8000)
    assert llm[:4] == ["-hf", "Qwen/Qwen3-8B-GGUF:Q4_K_M", "--alias", "Qwen/Qwen3-8B"]
    assert "--embedding" not in llm and "--rerank" not in llm
    # VLM: проектор картинок (mmproj) -hf скачивает сам, отдельных флагов не нужно
    assert server_args(Served("vision", VisionSettings()), 8003)[:4] == [
        "-hf", "prithivMLmods/Xiaomi-OCR-0-GGUF:Q6_K",
        "--alias", "prithivMLmods/Xiaomi-OCR-0-GGUF:Q6_K",
    ]  # fmt: skip


@pytest.fixture
def sleeper():
    """Настоящий процесс, чей pid изображает llama-server."""
    procs: list[subprocess.Popen] = []

    def spawn(args, **kwargs):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        procs.append(proc)
        spawn.calls.append(args)
        return proc

    spawn.calls = []
    yield spawn, procs
    for proc in procs:
        proc.kill()
        proc.wait()


def test_native_runner_starts_once_and_restarts_on_new_args(tmp_path, sleeper):
    spawn, procs = sleeper
    runner = NativeRunner(
        tmp_path / "state", tmp_path / "logs", lambda name: "/bin/llama-server", spawn
    )
    served = served_roles(settings())
    runner.start(served)
    runner.start(served)  # те же аргументы: уже работает
    assert len(spawn.calls) == 1 and spawn.calls[0][0] == "/bin/llama-server"
    assert runner.alive(served[0])
    record = json.loads((tmp_path / "state" / "embedder.json").read_text())
    assert record["pid"] == procs[0].pid

    changed = served_roles(settings(embedder=EmbedderSettings(server=ServerSettings(ctx=4096))))
    runner.start(changed)
    assert len(spawn.calls) == 2
    assert procs[0].wait(timeout=5) is not None  # старый сервер остановлен
    runner.prune([])  # эмбеддер стал api
    assert procs[1].wait(timeout=5) is not None
    assert not runner.alive(served[0])


def test_native_runner_needs_llama_server(tmp_path):
    with pytest.raises(ServeError, match="brew install llama.cpp"):
        NativeRunner(tmp_path, tmp_path, lambda name: None).start(served_roles(settings()))


class FakeRunner:
    def __init__(self, dead: set[str] = frozenset()):
        self.events: list[str] = []
        self.pruned: list[list[str]] = []
        self.dead = dead

    def prune(self, served):
        self.events.append("prune")
        self.pruned.append([s.role for s in served])

    def start(self, served):
        self.events.append("start " + ",".join(s.role for s in served))

    def alive(self, served):
        return served.role not in self.dead


def test_bring_up_starts_servers_one_by_one():
    full = settings(rerank=RerankSettings(enabled=True), llm=LOCAL_LLM)
    runner = FakeRunner()
    ready: set[str] = set()

    def is_ready(served):  # сервер готов со второй проверки
        if served.role in ready:
            return True
        ready.add(served.role)
        return False

    bring_up(served_roles(full), runner, 60, ready=is_ready, sleep=lambda s: None)
    assert runner.events == ["prune", "start embedder", "start rerank", "start llm"]


def test_bring_up_of_one_role_keeps_the_other_servers():
    full = settings(rerank=RerankSettings(enabled=True))
    runner = FakeRunner()
    bring_up(served_roles(full, ["rerank"]), runner, 60, keep=served_roles(full), ready=bool)
    assert runner.pruned == [["embedder", "rerank"]]
    assert runner.events == ["prune", "start rerank"]


def test_wait_ready_reports_exited_and_slow_servers():
    served = served_roles(settings())
    with pytest.raises(ServeError, match="embedder: llama-server exited"):
        wait_ready(served, FakeRunner(dead={"embedder"}), 60, ready=lambda s: False)
    clock = iter(range(0, 1000, 100))
    with pytest.raises(ServeError, match="embedder not ready after 150 s"):
        wait_ready(
            served,
            FakeRunner(),
            150,
            ready=lambda s: False,
            sleep=lambda s: None,
            clock=lambda: next(clock),
        )


@pytest.mark.parametrize(
    ("respond", "state"),
    [
        (lambda request: httpx.Response(200), "ready"),
        (lambda request: httpx.Response(503), "loading the model"),
        (lambda request: httpx.Response(500), "HTTP 500"),
        (lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused")),
         "starting or downloading the model"),
    ],
)  # fmt: skip
def test_health_state_says_what_the_server_is_doing(respond, state):
    [served] = served_roles(settings())
    client = httpx.Client(transport=httpx.MockTransport(respond))
    assert health_state(served, client) == state
    assert is_ready(served, client) == (state == "ready")
