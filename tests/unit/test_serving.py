import json
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

from mnogobase.config import (
    EmbedderSettings,
    LLMSettings,
    RerankSettings,
    ServerSettings,
    Settings,
    VisionSettings,
)
from mnogobase.serving import (
    COMPOSE_PROJECT,
    QWEN3_RERANKER_TEMPLATE,
    DockerRunner,
    NativeRunner,
    Served,
    ServeError,
    bring_up,
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
    assert [s.service for s in served_roles(full)] == [
        "embed-llamacpp",
        "rerank-llamacpp",
        "llm-llamacpp",
    ]
    assert [s.role for s in served_roles(full, ["llm"])] == ["llm"]
    with_vision = settings(vision=VisionSettings(enabled=True), llm=LOCAL_LLM)
    assert [s.service for s in served_roles(with_vision)] == [
        "embed-llamacpp",
        "vision-llamacpp",
        "llm-llamacpp",
    ]
    with pytest.raises(ServeError, match="unknown role nope"):
        served_roles(full, ["nope"])


def test_llamacpp_args_name_the_model_like_vllm():
    embed = Served("embedder", EmbedderSettings(server=ServerSettings(ctx=4096, args=["-np", "8"])))
    assert server_args(embed, 8080) == [
        "-hf", "Qwen/Qwen3-Embedding-4B-GGUF:Q4_K_M", "--alias", "Qwen/Qwen3-Embedding-4B",
        "--port", "8080", "--ctx-size", "4096", "--n-gpu-layers", "999", "--metrics",
        "--embedding", "--ubatch-size", "4096", "-np", "8",
    ]  # fmt: skip
    rerank = server_args(Served("rerank", RerankSettings()), 8002)
    assert "--rerank" in rerank and "--alias" in rerank and "Qwen/Qwen3-Reranker-4B" in rerank
    llm = server_args(Served("llm", LOCAL_LLM), 8000)
    assert "--embedding" not in llm and "--rerank" not in llm
    # VLM: проектор картинок (mmproj) -hf скачивает сам, отдельных флагов не нужно
    assert server_args(Served("vision", VisionSettings()), 8003)[:4] == [
        "-hf", "unsloth/Qwen3-VL-2B-Instruct-GGUF:Q4_K_M", "--alias", "Qwen/Qwen3-VL-2B-Instruct",
    ]  # fmt: skip


def test_vllm_args_serve_the_hf_repo():
    embed = server_args(Served("embedder", EmbedderSettings(serve="vllm")), 8000)
    assert embed == [
        "Qwen/Qwen3-Embedding-4B", "--port", "8000", "--max-model-len", "8192",
        "--gpu-memory-utilization", "0.15", "--runner", "pooling",
    ]  # fmt: skip
    rerank = server_args(Served("rerank", RerankSettings(serve="vllm")), 8000)
    overrides = json.loads(rerank[rerank.index("--hf-overrides") + 1])
    assert overrides["classifier_from_token"] == ["no", "yes"]
    assert rerank[rerank.index("--chat-template") + 1] == QWEN3_RERANKER_TEMPLATE
    other = server_args(Served("rerank", RerankSettings(serve="vllm", model="BAAI/bge")), 8000)
    assert "--hf-overrides" not in other  # обёртка только для Qwen3-Reranker
    llm = LOCAL_LLM.model_copy(update={"serve": "vllm"})
    assert "--runner" not in server_args(Served("llm", llm), 8000)


class FakeRun:
    def __init__(self, running: str = ""):
        self.calls: list[tuple[list[str], dict]] = []
        self.running = running

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        return SimpleNamespace(stdout=self.running, returncode=0)


def test_docker_runner_prunes_other_backends_and_passes_the_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPOSE_FILE", "docker-compose.yml")  # из .env сервера
    compose = tmp_path / "docker-compose.models.yml"
    compose.write_text("services: {}\n")
    run = FakeRun()
    runner = DockerRunner(compose, run=run)
    served = served_roles(settings(rerank=RerankSettings(enabled=True, serve="vllm")))
    runner.prune(served)
    runner.start(served)
    (rm, rm_kwargs), (up, up_kwargs) = run.calls
    assert rm[:6] == ["docker", "compose", "-p", COMPOSE_PROJECT, "-f", str(compose)]
    assert rm[rm.index("rm") :] == [
        "rm", "--stop", "--force", "embed-vllm", "rerank-llamacpp",
        "vision-llamacpp", "vision-vllm", "llm-llamacpp", "llm-vllm",
    ]  # fmt: skip
    assert up[up.index("up") :] == ["up", "-d", "embed-llamacpp", "rerank-vllm"]
    env = up_kwargs["env"]
    assert "COMPOSE_FILE" not in env
    assert shlex.split(env["MB_EMBED_LLAMACPP_ARGS"])[:2] == [
        "-hf",
        "Qwen/Qwen3-Embedding-4B-GGUF:Q4_K_M",
    ]
    assert "--port 8000" in env["MB_RERANK_VLLM_ARGS"]  # порт внутри контейнера vLLM


def test_docker_runner_needs_the_compose_file(tmp_path):
    with pytest.raises(ServeError, match="not found"):
        DockerRunner(tmp_path / "missing.yml")


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


def test_native_runner_refuses_vllm_and_needs_llama_server(tmp_path):
    vllm = served_roles(settings(embedder=EmbedderSettings(serve="vllm")))
    runner = NativeRunner(tmp_path, tmp_path, lambda name: "/bin/llama-server")
    with pytest.raises(ServeError, match="vLLM runs only in Docker"):
        runner.start(vllm)
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


def test_bring_up_starts_vllm_servers_one_by_one():
    full = settings(
        embedder=EmbedderSettings(serve="vllm"),
        rerank=RerankSettings(enabled=True),
        llm=LOCAL_LLM.model_copy(update={"serve": "vllm"}),
    )
    runner = FakeRunner()
    ready: set[str] = set()

    def is_ready(served):  # сервер готов со второй проверки
        if served.role in ready:
            return True
        ready.add(served.role)
        return False

    bring_up(served_roles(full), runner, 60, ready=is_ready, sleep=lambda s: None)
    assert runner.events == ["prune", "start rerank", "start embedder", "start llm"]


def test_bring_up_of_one_role_keeps_the_other_servers():
    full = settings(rerank=RerankSettings(enabled=True))
    runner = FakeRunner()
    bring_up(served_roles(full, ["rerank"]), runner, 60, keep=served_roles(full), ready=bool)
    assert runner.pruned == [["embedder", "rerank"]]
    assert runner.events == ["prune", "start rerank"]


def test_wait_ready_reports_exited_and_slow_servers():
    served = served_roles(settings())
    with pytest.raises(ServeError, match="embedder: the llamacpp server exited"):
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


def test_docker_runner_lists_the_model_caches_inside_the_container(tmp_path):
    compose = tmp_path / "docker-compose.models.yml"
    compose.write_text("services: {}\n")
    run = FakeRun(running="2500 /models/blobs/abc\n7 /root/.cache/huggingface/hub/x\n")
    [served] = served_roles(settings())
    assert DockerRunner(compose, run=run).cache_listing(served) == [
        (2500, "/models/blobs/abc"),
        (7, "/root/.cache/huggingface/hub/x"),
    ]
    [(cmd, kwargs)] = run.calls
    assert cmd[cmd.index("exec") :][:4] == ["exec", "-T", "embed-llamacpp", "find"]
    assert kwargs["check"] is False
