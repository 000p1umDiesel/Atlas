import io
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
from rich.console import Console

from mnogobase.config import EmbedderSettings, LLMSettings, Settings, VisionSettings
from mnogobase.model_top import Gpu, ModelTop, api_load, gpus, parse_metrics
from mnogobase.model_view import top_view

LLAMA_METRICS = """# HELP llamacpp:prompt_tokens_total Number of prompt tokens
# TYPE llamacpp:prompt_tokens_total counter
llamacpp:prompt_tokens_total {tokens}
llamacpp:tokens_predicted_total 0
llamacpp:requests_deferred 3
"""
VLLM_METRICS = """vllm:num_requests_running{model_name="vl"} 2.0
vllm:num_requests_waiting{model_name="vl"} 1.0
vllm:prompt_tokens_total{model_name="vl"} 900.0
vllm:generation_tokens_total{model_name="vl"} 300.0
"""
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


def test_prometheus_text_sums_labels_and_skips_comments():
    metrics = parse_metrics(VLLM_METRICS + 'vllm:prompt_tokens_total{model_name="b"} 100\nbad x\n')
    assert metrics["vllm:prompt_tokens_total"] == 1000.0
    assert metrics["vllm:num_requests_running"] == 2.0 and "bad" not in metrics


def _settings(tmp_path, **update):
    return Settings().model_copy(update={"logs_dir": tmp_path, **update})


def test_top_reads_llama_cpp_and_vllm_servers(tmp_path):
    tokens = iter([1000, 6000])

    def handler(request):
        if request.url.port == 8001 and request.url.path == "/metrics":
            return httpx.Response(200, text=LLAMA_METRICS.format(tokens=next(tokens)))
        if request.url.port == 8001 and request.url.path == "/slots":
            return httpx.Response(200, json=[{"is_processing": True}, {"is_processing": False}])
        if request.url.port == 8003:
            return httpx.Response(200, text=VLLM_METRICS)
        return httpx.Response(503)

    settings = _settings(
        tmp_path,
        vision=VisionSettings(enabled=True, serve="vllm"),
        llm=LLMSettings(model="q", serve="llamacpp", gguf="q-GGUF:Q4", base_url=None),
    )
    clock = iter([0.0, 0.0, 2.0, 2.0])  # опрос: embedder, vision
    top = ModelTop(
        settings,
        pid_of=lambda role: 42 if role == "embedder" else None,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: next(clock),
        gpu_query=lambda: [],
        memory_of=lambda pid: 3_900_000_000,
    )
    top.poll()
    loads, _ = top.poll()
    embedder, vision, llm = loads
    assert (embedder.state, embedder.busy, embedder.slots, embedder.waiting) == ("ready", 1, 2, 3)
    assert (
        embedder.tok_in == 2500 and embedder.total_in == 6000 and embedder.memory == 3_900_000_000
    )
    assert (vision.busy, vision.waiting, vision.total_out) == (2, 1, 300)
    assert llm.state == "down"


def test_api_llm_load_comes_from_the_log(tmp_path):
    def call(seconds_ago, tokens_in, ms):
        ts = (NOW - timedelta(seconds=seconds_ago)).isoformat().replace("+00:00", "Z")
        return json.dumps(
            {"event": "llm_call", "ts": ts, "tokens_in": tokens_in, "tokens_out": 10,
             "duration_ms": ms}
        )  # fmt: skip

    log = tmp_path / "mnogobase.jsonl"
    lines = [call(300, 9999, 1), call(30, 600, 2000), "{bad", call(5, 600, 4000)]
    log.write_text("\n".join(lines) + "\n")
    assert api_load(log, NOW) == (2, 1200, 20, 3.0)
    top = ModelTop(_settings(tmp_path), gpu_query=lambda: [], now=lambda: NOW)
    llm = top.poll()[0][-1]
    assert llm.state == "api" and llm.tok_in == 20 and llm.note == "2 calls/min, avg 3.0 s"


def test_gpus_from_nvidia_smi(monkeypatch):
    monkeypatch.setattr("mnogobase.model_top.shutil.which", lambda name: "/usr/bin/nvidia-smi")
    out = "0, NVIDIA L4, 71, 10035, 23034\n1, NVIDIA L4, [N/A], 1, 2\n"
    run = lambda *a, **k: SimpleNamespace(stdout=out)  # noqa: E731
    assert gpus(run) == [Gpu(0, "NVIDIA L4", 71, 10035, 23034)]


def test_top_view_shows_load_and_gpus():
    settings = Settings(embedder=EmbedderSettings())
    loads, _ = ModelTop(
        settings,
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))),
        gpu_query=lambda: [],
    ).poll()
    console = Console(width=120, file=io.StringIO(), color_system=None)
    console.print(top_view(loads, [Gpu(0, "NVIDIA L4", 71, 10035, 23034)], 120))
    text = console.file.getvalue()
    assert "embedder  llamacpp  ● down" in text
    assert "GPU 0 NVIDIA L4  util  71%  VRAM 9.8/22.5 GB" in text
