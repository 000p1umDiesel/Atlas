from pathlib import Path

import pytest

from mnogobase.config import DEFAULT_ENTITY_TYPES, ExtractSettings, load_settings


@pytest.fixture
def clean_env(monkeypatch):
    # setenv+delenv makes monkeypatch restore "absent" even if load_dotenv sets the var later
    for name in (
        "LLM_API_KEY",
        "NEO4J_PASSWORD",
        "MNOGOBASE_EMBEDDER__DIM",
        "MNOGOBASE_CONFIG",
        "MNOGOBASE_EXTRACT__ENTITY_TYPES",
    ):
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
    clean_env.chdir(tmp_path)
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


def test_explicit_missing_config_raises(tmp_path, clean_env):
    clean_env.chdir(tmp_path)
    missing = tmp_path / "nope.yaml"
    with pytest.raises(FileNotFoundError, match="nope.yaml"):
        load_settings(missing)


def test_env_config_missing_raises(tmp_path, clean_env):
    clean_env.chdir(tmp_path)
    clean_env.setenv("MNOGOBASE_CONFIG", str(tmp_path / "typo.yaml"))
    with pytest.raises(FileNotFoundError, match="typo.yaml"):
        load_settings()


def test_env_config_existing_is_used(tmp_path, clean_env):
    clean_env.chdir(tmp_path)
    cfg = tmp_path / "other.yaml"
    cfg.write_text("llm:\n  model: from-env-config\n", encoding="utf-8")
    clean_env.setenv("MNOGOBASE_CONFIG", str(cfg))
    assert load_settings().llm.model == "from-env-config"


TYPE_NAMES = [
    "Person",
    "Organization",
    "Location",
    "Event",
    "Project",
    "Product",
    "Software",
    "Technology",
    "AIModel",
    "Method",
    "Concept",
    "Field",
    "Work",
    "Dataset",
    "Metric",
    "Regulation",
    "Substance",
    "Condition",
    "Organism",
    "Other",
]


def test_default_entity_types_have_descriptions(tmp_path, clean_env):
    clean_env.chdir(tmp_path)
    s = load_settings()
    assert s.extract.type_names == TYPE_NAMES
    assert list(DEFAULT_ENTITY_TYPES) == TYPE_NAMES
    assert all(s.extract.entity_types[name].strip() for name in TYPE_NAMES)


def test_repo_config_uses_the_default_entity_types(tmp_path, clean_env):
    clean_env.chdir(tmp_path)
    s = load_settings(Path(__file__).parents[2] / "config.yaml")
    assert s.extract.entity_types == DEFAULT_ENTITY_TYPES


def test_entity_types_as_yaml_mapping_keep_order(tmp_path, clean_env):
    clean_env.chdir(tmp_path)
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "extract:\n  entity_types:\n    Gene: A named gene.\n    Drug: A medicine.\n"
        "    Bare:\n    Other: Anything else.\n",
        encoding="utf-8",
    )
    s = load_settings(cfg)
    assert s.extract.type_names == ["Gene", "Drug", "Bare", "Other"]
    assert s.extract.entity_types["Drug"] == "A medicine."
    assert s.extract.entity_types["Bare"] == ""


def test_entity_types_as_plain_list(tmp_path, clean_env):
    clean_env.chdir(tmp_path)
    cfg = tmp_path / "c.yaml"
    cfg.write_text("extract:\n  entity_types: [Person, Concept, Other]\n", encoding="utf-8")
    s = load_settings(cfg)
    assert s.extract.entity_types == {"Person": "", "Concept": "", "Other": ""}
    assert s.extract.type_names == ["Person", "Concept", "Other"]


def test_entity_types_env_override_as_list(tmp_path, clean_env):
    clean_env.chdir(tmp_path)
    clean_env.setenv("MNOGOBASE_EXTRACT__ENTITY_TYPES", '["Gene", "Other"]')
    assert load_settings().extract.entity_types == {"Gene": "", "Other": ""}


@pytest.mark.parametrize("value", [[], {}])
def test_entity_types_must_not_be_empty(value):
    with pytest.raises(ValueError, match="entity_types"):
        ExtractSettings(entity_types=value)
