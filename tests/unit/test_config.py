from pathlib import Path

import pytest

from mnogobase.config import load_settings


@pytest.fixture
def clean_env(monkeypatch):
    # setenv+delenv makes monkeypatch restore "absent" even if load_dotenv sets the var later
    for name in ("LLM_API_KEY", "NEO4J_PASSWORD", "MNOGOBASE_EMBEDDER__DIM", "MNOGOBASE_CONFIG"):
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)
    return monkeypatch


def test_defaults_without_config_file(tmp_path, clean_env):
    clean_env.chdir(tmp_path)
    s = load_settings()
    assert s.embedder.model == "embeddinggemma-2:740m"
    assert s.embedder.dim == 768
    assert s.qdrant.prefix == "mb_"
    assert s.llm.model == "gpt-6-luna"
    assert s.llm.model_for("extract") == "gpt-6-luna"
    assert s.wiki.dir == Path("wiki")
    assert s.retrieval.budget == {"rag": 0.4, "wiki": 0.3, "graph": 0.3}


def test_yaml_then_env_override(tmp_path, clean_env):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "llm:\n  model: m1\n  overrides:\n    wiki: m2\nembedder:\n  dim: 512\n",
        encoding="utf-8",
    )
    clean_env.setenv("MNOGOBASE_EMBEDDER__DIM", "256")
    s = load_settings(cfg)
    assert s.llm.model == "m1"
    assert s.llm.model_for("wiki") == "m2"
    assert s.llm.model_for("extract") == "m1"
    assert s.embedder.dim == 256


def test_secrets_from_dotenv(tmp_path, clean_env):
    clean_env.chdir(tmp_path)
    (tmp_path / ".env").write_text("LLM_API_KEY=sk-test\nNEO4J_PASSWORD=pw\n", encoding="utf-8")
    s = load_settings()
    assert s.llm.api_key() == "sk-test"
    assert s.neo4j.password() == "pw"


def test_api_key_placeholder_for_ollama(tmp_path, clean_env):
    clean_env.chdir(tmp_path)
    assert load_settings().llm.api_key() == "ollama"
