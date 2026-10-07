import httpx
import pytest

from mnogobase import doctor
from mnogobase.config import EmbedderSettings, Settings
from mnogobase.doctor import run_checks
from mnogobase.registry import Registry


def make_settings(tmp_path, **update) -> Settings:
    return Settings().model_copy(update={"data_dir": tmp_path / ".mb", **update})


def store_signature(tmp_path, signature: str) -> None:
    registry = Registry(tmp_path / ".mb" / "state.db")
    registry.set_meta("embedder", signature)
    registry.close()


def test_index_check_compares_with_the_configured_embedder(tmp_path):
    settings = make_settings(tmp_path)
    [fresh] = run_checks(settings, only=["index"])
    assert fresh.ok and fresh.detail == "no index yet"
    assert not (tmp_path / ".mb" / "state.db").exists()

    store_signature(tmp_path, "ollama:embeddinggemma-2:740m:768")
    [same] = run_checks(settings, only=["index"])
    assert same.ok

    store_signature(tmp_path, "ollama:qwen3-embedding:0.6b:1024")
    [changed] = run_checks(settings, only=["index"])
    assert not changed.ok
    assert "qwen3-embedding:0.6b:1024" in changed.detail
    assert "ollama:embeddinggemma-2:740m:768" in changed.detail
    assert "mnogobase reindex" in changed.detail


def test_unreachable_service_is_a_failed_check_not_a_crash(tmp_path):
    settings = make_settings(tmp_path, embedder=EmbedderSettings(base_url="http://127.0.0.1:9"))
    checks = run_checks(settings, timeout=1.0, only=["index", "ollama"])
    assert [c.name for c in checks] == ["ollama", "index"]  # doctor order, not request order
    assert not checks[0].ok and "ConnectError" in checks[0].detail


def _models_endpoint(monkeypatch, status: int, payload: dict | None = None) -> list[str]:
    seen: list[str] = []

    def fake_get(url, **kwargs):
        seen.append(url)
        return httpx.Response(status, json=payload or {}, request=httpx.Request("GET", url))

    monkeypatch.setattr(doctor.httpx, "get", fake_get)
    return seen


@pytest.mark.parametrize("status", [404, 405])
def test_llm_without_models_endpoint_is_ok(tmp_path, monkeypatch, status):
    seen = _models_endpoint(monkeypatch, status)
    [check] = run_checks(make_settings(tmp_path), only=["llm"])
    assert seen == ["https://codex.sale/v1/models"]
    assert check.ok, check.detail
    assert "models endpoint not available" in check.detail


@pytest.mark.parametrize("status", [401, 403, 500])
def test_llm_auth_or_server_error_fails(tmp_path, monkeypatch, status):
    _models_endpoint(monkeypatch, status)
    [check] = run_checks(make_settings(tmp_path), only=["llm"])
    assert not check.ok and str(status) in check.detail


def test_llm_model_listed_or_empty_list_is_ok(tmp_path, monkeypatch):
    _models_endpoint(monkeypatch, 200, {"data": [{"id": "gpt-6-luna"}]})
    assert run_checks(make_settings(tmp_path), only=["llm"])[0].ok
    _models_endpoint(monkeypatch, 200, {"data": []})
    assert run_checks(make_settings(tmp_path), only=["llm"])[0].ok
