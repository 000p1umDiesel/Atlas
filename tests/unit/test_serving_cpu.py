"""CPU selection and real orchestration with Docker replaced at its subprocess boundary."""

import shlex
import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from mnogobase import cli, serving
from mnogobase.config import EmbedderSettings, RerankSettings, ServerSettings, Settings
from mnogobase.serving import DockerRunner, ServeError, bring_up, served_roles, server_args


class ModelDocker:
    def __init__(self, failures=None):
        self.failures = failures or {}
        self.containers = {}
        self.calls = []
        self.started = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        index = next(
            i for i, arg in enumerate(cmd) if arg in {"up", "rm", "ps", "logs", "stop", "down"}
        )
        operation, *args = cmd[index:]
        services = [arg for arg in args if not arg.startswith("-")]
        stdout = ""
        if operation == "up":
            for name in services:
                self.started.append(name)
                failure = self.failures.get(name)
                if failure and failure[0] == "docker":
                    raise subprocess.CalledProcessError(1, cmd, stderr=failure[1])
                self.containers[name] = not failure
        elif operation == "rm":
            for name in services:
                self.containers.pop(name, None)
        elif operation == "stop":
            for name in services:
                if name in self.containers:
                    self.containers[name] = False
        elif operation == "ps":
            name = args[-1]
            if name in self.containers and ("--all" in args or self.containers[name]):
                stdout = "container-id\n"
        elif operation == "logs":
            stdout = self.failures.get(args[-1], ("", ""))[1]
        elif operation == "down":
            self.containers.clear()
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")


def docker_runner(tmp_path, failures=None):
    compose = tmp_path / "docker-compose.models.yml"
    compose.write_text("services: {}\n")
    docker = ModelDocker(failures)
    return DockerRunner(compose, run=docker), docker


def test_cpu_device_selects_cpu_services_and_preserves_weights():
    backend = "llamacpp"
    model = "example/embedding-GGUF:Q4_K_M"
    config = Settings(device="cpu", embedder=EmbedderSettings(serve=backend, model=model))
    [served] = served_roles(config)
    assert served.service == f"embed-{backend}-cpu"
    assert served.settings.weights == model
    assert served.settings.url == "http://localhost:8001/v1"
    args = server_args(served, 8080 if backend == "llamacpp" else 8000)
    assert args[args.index("--n-gpu-layers") + 1] == "0"


def test_vllm_rejects_cpu_configuration_before_touching_containers(tmp_path):
    runner, docker = docker_runner(tmp_path)
    config = Settings(
        device="cpu", embedder=EmbedderSettings(serve="vllm", model="example/embedding")
    )
    with pytest.raises(ServeError, match="vLLM requires CUDA"):
        bring_up(served_roles(config), runner, 10)
    assert docker.calls == []


def test_cpu_mode_cannot_be_overridden_by_extra_gpu_layers():
    config = Settings(
        device="cpu",
        embedder=EmbedderSettings(server=ServerSettings(args=["--n-gpu-layers", "999"])),
    )
    [served] = served_roles(config)
    args = server_args(served, 8080)
    assert args[-2:] == ["--n-gpu-layers", "0"]


def test_cpu_device_uses_docker_in_auto_mode_on_mac(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docker-compose.models.yml").write_text("services: {}\n")
    monkeypatch.setattr(serving.sys, "platform", "darwin")
    assert isinstance(serving.make_runner("auto", Settings(device="cpu")), DockerRunner)


@pytest.mark.parametrize(
    "failure",
    [
        ("docker", 'could not select device driver "nvidia" with capabilities: [[gpu]]'),
        (
            "server",
            "ggml_backend_cuda_buffer_type_alloc_buffer: allocating 1024 MiB: CUDA out of memory",
        ),
        ("server", "CUDA error: no kernel image is available for execution on the device"),
    ],
)
def test_failed_gpu_role_is_retried_once_on_cpu(tmp_path, failure):
    runner, docker = docker_runner(tmp_path, {"embed-llamacpp": failure})
    notices = []
    bring_up(
        served_roles(Settings()),
        runner,
        10,
        ready=lambda s: docker.containers.get(s.service, False),
        on_fallback=lambda s, reason: notices.append((s.role, reason)),
    )
    assert docker.started == ["embed-llamacpp", "embed-llamacpp-cpu"]
    assert docker.containers == {"embed-llamacpp-cpu": True}
    assert len(notices) == 1 and notices[0][0] == "embedder"
    cpu_up = next(
        kwargs for cmd, kwargs in docker.calls if cmd[-3:] == ["up", "-d", "embed-llamacpp-cpu"]
    )
    args = shlex.split(cpu_up["env"]["MB_EMBED_LLAMACPP_CPU_ARGS"])
    assert args[:2] == ["-hf", "Qwen/Qwen3-Embedding-4B-GGUF:Q4_K_M"]
    assert args[-2:] == ["--n-gpu-layers", "0"]


@pytest.mark.parametrize(
    "failure",
    [
        ("docker", "Cannot connect to the Docker daemon"),
        ("docker", "port is already allocated"),
        ("server", "error loading model: invalid model architecture"),
        ("server", "HTTP 401 Unauthorized downloading weights"),
        (
            "server",
            "ggml_cuda_init: GGML_CUDA_FORCE_CUBLAS: no\nHTTP 401 Unauthorized downloading weights",
        ),
        ("server", "Loaded libcuda.so successfully\nerror loading model: invalid architecture"),
    ],
)
def test_non_gpu_errors_do_not_trigger_cpu_fallback(tmp_path, failure):
    runner, docker = docker_runner(tmp_path, {"embed-llamacpp": failure})
    with pytest.raises(ServeError):
        bring_up(
            served_roles(Settings()),
            runner,
            10,
            ready=lambda s: docker.containers.get(s.service, False),
        )
    assert docker.started == ["embed-llamacpp"]


def test_cpu_failure_stops_retrying_and_stops_restart_loop(tmp_path):
    runner, docker = docker_runner(
        tmp_path,
        {
            "embed-llamacpp": ("server", "CUDA out of memory"),
            "embed-llamacpp-cpu": ("server", "CPU allocation failed"),
        },
    )
    with pytest.raises(ServeError, match="CPU"):
        bring_up(
            served_roles(Settings()),
            runner,
            10,
            ready=lambda s: docker.containers.get(s.service, False),
        )
    assert docker.started == ["embed-llamacpp", "embed-llamacpp-cpu"]
    assert not any(docker.containers.values())
    assert any("stop" in cmd and cmd[-1] == "embed-llamacpp-cpu" for cmd, _ in docker.calls)


def test_vllm_memory_budget_failure_does_not_retry_on_cpu(tmp_path):
    runner, docker = docker_runner(
        tmp_path,
        {
            "embed-vllm": (
                "server",
                "ValueError: Free memory on device (1/8 GiB) on startup is less than desired GPU memory utilization (0.5, 4 GiB).",
            ),
        },
    )
    config = Settings(embedder=EmbedderSettings(serve="vllm", model="example/embedding"))
    with pytest.raises(ServeError):
        bring_up(
            served_roles(config),
            runner,
            10,
            ready=lambda s: docker.containers.get(s.service, False),
        )
    assert docker.started == ["embed-vllm"]
    assert docker.containers == {"embed-vllm": False}


def test_slow_loading_gpu_server_does_not_switch_to_cpu(tmp_path):
    runner, docker = docker_runner(tmp_path)
    clock = iter((0, 2))
    with pytest.raises(ServeError, match="not ready after"):
        bring_up(
            served_roles(Settings()),
            runner,
            1,
            ready=lambda s: False,
            clock=lambda: next(clock),
            sleep=lambda seconds: None,
        )
    assert docker.started == ["embed-llamacpp"]
    assert docker.containers == {"embed-llamacpp": False}


def test_healthy_gpu_roles_start_sequentially_without_cpu_fallback(tmp_path):
    runner, docker = docker_runner(tmp_path)
    config = Settings(rerank=RerankSettings(enabled=True))
    bring_up(
        served_roles(config), runner, 10, ready=lambda s: docker.containers.get(s.service, False)
    )
    assert docker.started == ["embed-llamacpp", "rerank-llamacpp"]
    assert all(docker.containers.values())


def test_cpu_start_preserves_other_roles_and_logs_follow_cpu_container(tmp_path):
    runner, docker = docker_runner(tmp_path)
    docker.containers = {"rerank-llamacpp": True, "embed-llamacpp": True}
    config = Settings(device="cpu", rerank=RerankSettings(enabled=True))
    bring_up(
        served_roles(config, ["embedder"]),
        runner,
        10,
        keep=served_roles(config),
        ready=lambda s: docker.containers.get(s.service, False),
    )
    assert docker.containers == {"rerank-llamacpp": True, "embed-llamacpp-cpu": True}
    # A new invocation uses GPU settings again; logs must still find the fallback container.
    runner.logs(served_roles(Settings())[0], follow=False)
    assert docker.calls[-1][0][-1] == "embed-llamacpp-cpu"
    runner.stop()
    assert docker.containers == {}


def test_cpu_services_keep_the_existing_caches_and_have_no_gpu_request():
    compose = yaml.safe_load((Path(__file__).parents[2] / "docker-compose.models.yml").read_text())
    for role in ("embed", "rerank", "vision", "llm"):
        for backend, volume in (("llamacpp", "llama_models:/models"),):
            service = compose["services"].get(f"{role}-{backend}-cpu", {})
            assert volume in service.get("volumes", [])
            assert (
                not service.get("deploy", {})
                .get("resources", {})
                .get("reservations", {})
                .get("devices")
            )
            assert service.get("mem_limit") and service.get("cpus")


def test_cli_rejects_cpu_vllm_with_a_clean_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(
        "device: cpu\nembedder: {serve: vllm, model: example/embedding}\n"
    )
    (tmp_path / "docker-compose.models.yml").write_text("services: {}\n")
    monkeypatch.setattr(cli, "bring_up", lambda *a, **kw: pytest.fail("invalid CPU vLLM started"))
    result = CliRunner().invoke(cli.app, ["models", "up"])
    assert result.exit_code == 2, result.output
    assert "vLLM requires CUDA" in result.output


def test_cli_reports_cpu_container_selection(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("device: cpu\n")
    (tmp_path / "docker-compose.models.yml").write_text("services: {}\n")
    monkeypatch.setattr(cli, "bring_up", lambda *a, **kw: None)
    result = CliRunner().invoke(cli.app, ["models", "up"])
    assert result.exit_code == 0, result.output
    assert "docker, cpu" in result.output
    assert "--n-gpu-layers 0" in result.output


def test_cli_reports_gpu_fallback_with_the_role_and_reason(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("device: auto\n")
    (tmp_path / "docker-compose.models.yml").write_text("services: {}\n")

    def launch(served, runner, timeout, on_wait, keep, on_fallback):
        on_fallback(served[0], "CUDA out of memory")

    monkeypatch.setattr(cli, "bring_up", launch)
    result = CliRunner().invoke(cli.app, ["models", "up", "--runner", "docker"])
    assert result.exit_code == 0, result.output
    assert "embedder" in result.output and "CPU" in result.output
    assert "CUDA out of memory" in result.output
