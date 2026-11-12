from pathlib import Path

import pytest

from mnogobase.config import (
    DEFAULT_ENTITY_TYPES,
    EmbedderSettings,
    ExtractSettings,
    LLMSettings,
    VisionSettings,
    load_settings,
)


@pytest.fixture
def clean_env(monkeypatch):
    # setenv+delenv: monkeypatch вернёт "отсутствует", даже если load_dotenv задаст переменную позже
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
    assert s.embedder.model == "Qwen/Qwen3-Embedding-4B-GGUF:Q4_K_M" and s.embedder.gguf is None
    assert s.embedder.serve == "llamacpp" and s.llm.serve == "api"
    assert s.embedder.dim == "auto"
    assert s.embedder.url == "http://localhost:8001/v1"
    assert s.rerank.url == "http://localhost:8002/v1"
    # без явного config.yaml документы не уходят во внешний API
    assert s.llm.url == "http://localhost:8000/v1"
    assert s.qdrant.prefix == "mb_"
    assert s.llm.model == "local"
    assert s.llm.model_for("extract") == "local"
    assert (s.jobs.max_attempts, s.jobs.retry_delay_s) == (5, 30.0)
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


def test_api_key_placeholder_for_local_servers(tmp_path, clean_env):
    clean_env.chdir(tmp_path)
    assert load_settings().llm.api_key() == "local"


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
    "Equipment",
    "Software",
    "Technology",
    "Method",
    "Concept",
    "Work",
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
    assert list(s.extract.entity_types) == TYPE_NAMES
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
    assert list(s.extract.entity_types) == ["Gene", "Drug", "Bare", "Other"]
    assert s.extract.entity_types["Drug"] == "A medicine."
    assert s.extract.entity_types["Bare"] == ""


def test_entity_types_as_plain_list(tmp_path, clean_env):
    clean_env.chdir(tmp_path)
    cfg = tmp_path / "c.yaml"
    cfg.write_text("extract:\n  entity_types: [Person, Concept, Other]\n", encoding="utf-8")
    s = load_settings(cfg)
    assert s.extract.entity_types == {"Person": "", "Concept": "", "Other": ""}


def test_entity_types_env_override_as_list(tmp_path, clean_env):
    clean_env.chdir(tmp_path)
    clean_env.setenv("MNOGOBASE_EXTRACT__ENTITY_TYPES", '["Gene", "Other"]')
    assert load_settings().extract.entity_types == {"Gene": "", "Other": ""}


@pytest.mark.parametrize("value", [[], {}])
def test_entity_types_must_not_be_empty(value):
    with pytest.raises(ValueError, match="entity_types"):
        ExtractSettings(entity_types=value)


YAML_TYPES = "extract:\n  entity_types:\n    Person: A human.\n    Other: Anything else.\n"


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        (
            '{"Gene": "A gene.", "Other": "Else."}',
            {"Gene": "A gene.", "Other": "Else."},
        ),
        ('["Gene", "Other"]', {"Gene": "", "Other": ""}),
    ],
)
def test_env_entity_types_replace_the_yaml_mapping(tmp_path, clean_env, env, expected):
    clean_env.chdir(tmp_path)
    cfg = tmp_path / "c.yaml"
    cfg.write_text(YAML_TYPES + "  max_failed_ratio: 0.5\n", encoding="utf-8")
    clean_env.setenv("MNOGOBASE_EXTRACT__ENTITY_TYPES", env)
    s = load_settings(cfg)
    assert s.extract.entity_types == expected
    assert list(s.extract.entity_types) == list(expected)  # порядок из env, Other последним
    assert s.extract.max_failed_ratio == 0.5  # остальная часть секции YAML по-прежнему действует


def test_yaml_list_of_one_key_mappings(tmp_path, clean_env):
    clean_env.chdir(tmp_path)
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "extract:\n  entity_types:\n    - Person: A human.\n    - Concept\n"
        "    - Other: Anything else.\n",
        encoding="utf-8",
    )
    s = load_settings(cfg)
    assert s.extract.entity_types == {
        "Person": "A human.",
        "Concept": "",
        "Other": "Anything else.",
    }


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        (["Person", "person"], "duplicate"),
        ({"Person": "a", "PERSON": "b"}, "duplicate"),
        (["Person", "  "], "blank"),
        ({"": "x", "Other": ""}, "blank"),
        (["Per:son", "Other"], "':'"),
        (["Per\nson", "Other"], "newline"),
        ([{"Person": "a", "Other": "b"}], "one key"),
    ],
)
def test_bad_entity_type_names_are_rejected(value, problem):
    with pytest.raises(ValueError, match=problem):
        ExtractSettings(entity_types=value)


def test_vision_skip_takes_docling_picture_classes():
    assert VisionSettings(skip=["logo", "bar_chart"]).skip == ["logo", "bar_chart"]
    with pytest.raises(ValueError, match="unknown picture class logotype"):
        VisionSettings(skip=["logotype"])


def test_serve_requires_what_the_backend_needs():
    with pytest.raises(ValueError, match="embedder.base_url is required for serve: api"):
        EmbedderSettings(serve="api")
    local = LLMSettings(model="Qwen/Qwen3-8B", serve="llamacpp", base_url=None)
    assert local.url == "http://localhost:8000/v1" and local.weights == "Qwen/Qwen3-8B"
    remote = EmbedderSettings(base_url="http://gpu:8001/v1/")  # модель поднята на другой машине
    assert remote.url == "http://gpu:8001/v1"
    assert remote.weights == "Qwen/Qwen3-Embedding-4B-GGUF:Q4_K_M"


@pytest.mark.parametrize(
    "text",
    [
        "llm:\n  modle: x\n",  # опечатка во вложенной секции
        "retrival:\n  k: 3\n",  # опечатка в имени секции
        "llm:\n  concurrency: 0\n",
        "embedder:\n  batch_size: 0\n",
        "retrieval:\n  budget: {rag: 0.7, wiki: 0.7}\n",
        "retrieval:\n  budget: {rag: 0.5, vector: 0.5}\n",
        'embedder:\n  query_template: "Query: {question}"\n',
        'embedder:\n  doc_template: "{title}"\n',
    ],
)
def test_invalid_config_is_rejected(tmp_path, clean_env, text):
    clean_env.chdir(tmp_path)
    cfg = tmp_path / "c.yaml"
    cfg.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_settings(cfg)


def test_repo_config_is_valid(clean_env):
    clean_env.chdir(Path(__file__).resolve().parents[2])
    load_settings()
