# mnogobase Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Python package + CLI that ingests RU/EN documents (Docling) into Qdrant (dense EmbeddingGemma 2 + sparse BM25) and a Neo4j knowledge graph, builds an incremental Karpathy-style markdown wiki, and answers questions with citations in 4 retrieval modes (`rag`, `wiki`, `graph`, `all`).

**Architecture:** Small modules behind narrow interfaces (`Embedder`, `SparseEncoder`, `LLMClient`, `QdrantStore`, `GraphStore`, `Registry`) wired together in `app.py`. Ingest is a resumable 5-stage pipeline (`parse → chunk → embed → extract → graph`) tracked in SQLite, with deterministic IDs and upserts so every stage is idempotent. The wiki is regenerated only for "dirty" entities; retrieval modes share one answer generator so results are comparable.

**Tech Stack:** Python 3.12, uv, Docling, qdrant-client (Qdrant 1.19), fastembed (BM25), neo4j driver 6 (Neo4j 5.26), openai SDK (any OpenAI-compatible endpoint), httpx, Typer + Rich, structlog, pydantic / pydantic-settings, tenacity, filelock, pytest + testcontainers.

**Spec:** `docs/superpowers/specs/2026-10-07-mnogobase-ingest-design.md`

## Global Constraints

- Python `>=3.12,<3.13`; package managed with `uv`; src layout `src/mnogobase`.
- Docker images: `qdrant/qdrant:v1.19.2`, `neo4j:5.26-community` (tests use the same Neo4j image via testcontainers).
- Default LLM: `base_url: https://codex.sale/v1`, `model: gpt-6-luna`, key from env `LLM_API_KEY` (in `.env`, never committed). Ollama alternative: `http://localhost:11434/v1`.
- Default embedder: Ollama `embeddinggemma-2:740m`, `dim: 768`, templates `title: {title} | text: {text}` / `task: search result | query: {query}`. Fallback: `qwen3-embedding:0.6b`, `dim: 1024`, `doc_template: "{text}"`, `query_template: "Instruct: Given a question, retrieve passages that answer it\nQuery: {query}"`.
- Structured output always uses strict JSON Schema: `"strict": true`, every object `additionalProperties: false`, every property listed in `required`, no `default`/`title` keywords.
- IDs: `doc_id = sha256(file)[:16]`; `chunk_id = f"{doc_id}:{idx:05d}"`; `entity_id = sha1(f"{type.casefold()}|{normalize(name)}")[:16]`; `page_id = entity_id`; Qdrant point id = `uuid5(NAMESPACE, key)`.
- Qdrant collections: `{prefix}chunks`, `{prefix}entities`, `{prefix}wiki_pages`; default prefix `mb_`; vector names `dense` and `bm25`.
- Wiki language default `en`; wiki files under `wiki/entities/<slug>.md`, plus `wiki/index.md`, `wiki/log.md`.
- State: `.mnogobase/state.db` (SQLite), caches in `.mnogobase/cache/`; logs `logs/mnogobase.jsonl`; comparisons `runs/compare.jsonl`.
- Logging only through `mnogobase.log.get_logger`; CLI output through Rich `Console`. No `print` in library code.
- Integration tests need a running Docker daemon (`-m integration`); e2e tests need Docker + Ollama + LLM endpoint (`-m e2e`). Default `pytest` runs unit tests only.
- Never write the API key into any committed file; `.env` is in `.gitignore`.

## Review Focus

- Same file content at two paths, then one path changes → the other path's chunks/entities must survive (pinned in Task 14 `test_duplicate_content_survives_change`).
- Entity names or questions with Lucene special characters (`C++`, `AND`, `(`, `/`, `"`) passed to Neo4j fulltext → no Cypher/Lucene error (pinned in Task 9 `test_fulltext_special_characters`).
- Empty document or document producing zero chunks → ingested as done with zero chunks, no division by zero in the extraction failure ratio (pinned in Task 14 `test_empty_document`).
- LLM replies wrapped in ```` ```json ```` fences or `<think>…</think>` blocks, or JSON with an invalid field → parsed or repaired, never crash the batch (pinned in Task 5 `test_structured_strips_fences_and_think` and `test_structured_repairs_invalid_json`).
- LLM returns relations whose endpoints are not in its own entity list, use an alias, or point to themselves → alias resolved, others dropped (pinned in Task 10 `test_clean_extraction_relations`).

---

## File Structure

```
pyproject.toml                         # deps, scripts, pytest/ruff config
config.yaml                            # default non-secret settings
.env.example                           # LLM_API_KEY, NEO4J_PASSWORD
docker-compose.yml                     # qdrant + neo4j(+APOC, GDS)
docker-compose.cuda.yml                # NVIDIA override: qdrant gpu image + ollama container
README.md
src/mnogobase/
  __init__.py
  config.py                            # Settings (YAML + env), load_settings()
  device.py                            # detect_device()
  log.py                               # configure_logging(), get_logger(), new_run_id(), log_stage()
  ids.py                               # deterministic ids, normalize_name(), slugify()
  models.py                            # pydantic domain models
  registry.py                          # SQLite state + ingest_lock()
  llm/
    __init__.py
    client.py                          # LLMClient protocol, OpenAICompatLLM, strict_json_schema(), clean_json()
    templates.py                       # render(name, **vars) for prompts/*.md
    prompts/extract.md
    prompts/resolve_same.md
    prompts/resolve_summarize.md
    prompts/wiki_page.md
    prompts/answer.md
  embedding/
    __init__.py
    base.py                            # Embedder / SparseEncoder protocols, templates, truncate_normalize()
    ollama.py                          # OllamaEmbedder
    sparse.py                          # BM25Encoder (fastembed)
  parsing/
    __init__.py
    docling_parser.py                  # DoclingParser, ParsedDocument
  chunking/
    __init__.py
    hybrid.py                          # Chunker (Docling HybridChunker)
  stores/
    __init__.py
    qdrant_store.py                    # QdrantStore
    graph_store.py                     # GraphStore (Neo4j), DeleteResult
  extraction/
    __init__.py
    extractor.py                       # Extractor, clean_extraction()
    resolver.py                        # EntityResolver
  wiki/
    __init__.py
    validate.py                        # citations / links / reserved sections
    render.py                          # page/index/log rendering, parse_page(), split_sections()
    builder.py                         # WikiBuilder
  retrieval/
    __init__.py                        # Mode, build_retrievers()
    base.py                            # Retriever protocol, estimate_tokens()
    rag.py  wiki.py  graph.py  combined.py
    answer.py                          # Answerer, build_context()
    compare.py                         # compare()
  pipeline.py                          # Pipeline, IngestReport
  app.py                               # App, build_app()
  maintenance.py                       # reindex(), reset()
  doctor.py                            # run_checks()
  cli.py                               # Typer app
tests/
  __init__.py
  fakes.py                             # FakeLLM, FakeEmbedder, FakeSparse, scripted_llm_handler()
  conftest.py                          # neo4j/graph fixtures (integration)
  fixtures/attention_en.md
  fixtures/vnimanie_ru.md
  unit/__init__.py      unit/test_*.py
  integration/__init__.py integration/test_*.py
  e2e/__init__.py       e2e/test_e2e.py
```

---

### Task 1: Project scaffold, configuration, Docker services

**Files:**
- Create: `pyproject.toml`, `config.yaml`, `.env.example`, `docker-compose.yml`, `docker-compose.cuda.yml`
- Create: `src/mnogobase/__init__.py`, `src/mnogobase/config.py`
- Create: `tests/__init__.py`, `tests/unit/__init__.py`, `tests/integration/__init__.py`, `tests/e2e/__init__.py`
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `mnogobase.config.Settings` with nested `LLMSettings`, `EmbedderSettings`, `SparseSettings`, `ParsingSettings`, `ChunkingSettings`, `ExtractSettings`, `ResolveSettings`, `WikiSettings`, `RetrievalSettings`, `GraphSettings`, `QdrantSettings`, `Neo4jSettings`; `load_settings(config_path: Path | None = None) -> Settings`; `LLMSettings.model_for(task: str) -> str`; `LLMSettings.api_key() -> str`; `Neo4jSettings.password() -> str`; `LANGUAGE_NAMES: dict[str, str]`.

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "mnogobase"
version = "0.1.0"
description = "LLM Wiki core: ingest documents into Qdrant + Neo4j, build a persistent wiki, answer with RAG / Wiki / Graph retrieval"
readme = "README.md"
requires-python = ">=3.12,<3.13"
dependencies = [
  "docling>=2.134",
  "qdrant-client>=1.19.1",
  "fastembed>=0.8.1",
  "neo4j>=6.4",
  "openai>=3.26",
  "httpx>=0.28",
  "typer>=0.27",
  "rich>=15.0",
  "structlog>=26.1",
  "pydantic>=2.13",
  "pydantic-settings[yaml]>=2.15",
  "python-dotenv>=1.1",
  "tenacity>=9.1",
  "filelock>=3.20",
  "pyyaml>=6.0",
  "transformers>=5.0",
]

[project.scripts]
mnogobase = "mnogobase.cli:app"

[dependency-groups]
dev = [
  "pytest>=9.1",
  "pytest-asyncio>=1.4",
  "testcontainers[neo4j,qdrant]>=4.15",
  "reportlab>=4.4",
  "ruff>=0.16",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/mnogobase"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
asyncio_mode = "auto"
markers = [
  "integration: needs a running Docker daemon (testcontainers)",
  "e2e: needs Docker + Ollama + the LLM endpoint",
]
addopts = "-m 'not integration and not e2e'"

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]
ignore = ["E501"]  # long lines are handled by `ruff format`
```

- [ ] **Step 2: Write the failing config test**

`tests/unit/test_config.py`:

```python
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
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv sync && uv run pytest tests/unit/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mnogobase'` (or `mnogobase.config`).

- [ ] **Step 4: Implement `src/mnogobase/__init__.py` and `src/mnogobase/config.py`**

`src/mnogobase/__init__.py`:

```python
"""mnogobase — LLM Wiki core."""

__version__ = "0.1.0"
```

`src/mnogobase/config.py`:

```python
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict, YamlConfigSettingsSource

LLM_TASKS = ("extract", "resolve", "wiki", "answer")
LANGUAGE_NAMES = {"en": "English", "ru": "Russian"}
DEFAULT_ENTITY_TYPES = [
    "Person", "Organization", "Concept", "Method", "Technology",
    "Dataset", "Work", "Event", "Location", "Other",
]
DEFAULT_EXTENSIONS = ["pdf", "docx", "pptx", "xlsx", "html", "htm", "md", "adoc", "csv", "txt"]


class LLMSettings(BaseModel):
    base_url: str = "https://codex.sale/v1"
    model: str = "gpt-6-luna"
    api_key_env: str = "LLM_API_KEY"
    concurrency: int = 4
    temperature: float = 0.0
    timeout_s: float = 120.0
    overrides: dict[str, str | None] = Field(default_factory=lambda: dict.fromkeys(LLM_TASKS))

    def model_for(self, task: str) -> str:
        return self.overrides.get(task) or self.model

    def api_key(self) -> str:
        # Ollama ignores the key, but the OpenAI SDK refuses an empty one.
        return os.environ.get(self.api_key_env) or "ollama"


class EmbedderSettings(BaseModel):
    provider: Literal["ollama"] = "ollama"
    base_url: str = "http://localhost:11434"
    model: str = "embeddinggemma-2:740m"
    dim: int = 768
    batch_size: int = 32
    doc_template: str = "title: {title} | text: {text}"
    query_template: str = "task: search result | query: {query}"


class SparseSettings(BaseModel):
    model: str = "Qdrant/bm25"


class ParsingSettings(BaseModel):
    ocr: bool = True
    extensions: list[str] = Field(default_factory=lambda: list(DEFAULT_EXTENSIONS))


class ChunkingSettings(BaseModel):
    tokenizer: str = "google/embeddinggemma-2"
    max_tokens: int = 512


class ExtractSettings(BaseModel):
    entity_types: list[str] = Field(default_factory=lambda: list(DEFAULT_ENTITY_TYPES))
    max_failed_ratio: float = 0.2


class ResolveSettings(BaseModel):
    auto_merge: float = 0.92
    llm_check: float = 0.80
    max_descriptions: int = 5


class WikiSettings(BaseModel):
    dir: Path = Path("wiki")
    language: str = "en"
    min_mentions: int = 2
    evidence_k: int = 12


class RetrievalSettings(BaseModel):
    k: int = 8
    context_tokens: int = 6000
    budget: dict[str, float] = Field(
        default_factory=lambda: {"rag": 0.4, "wiki": 0.3, "graph": 0.3}
    )


class GraphSettings(BaseModel):
    hops: int = 2
    max_relations: int = 30
    seeds: int = 5
    seed_threshold: float = 0.3  # min cosine for query -> entity seeds


class QdrantSettings(BaseModel):
    url: str = "http://localhost:6333"
    prefix: str = "mb_"


class Neo4jSettings(BaseModel):
    uri: str = "bolt://localhost:7687"
    user: str = "neo4j"
    password_env: str = "NEO4J_PASSWORD"

    def password(self) -> str:
        return os.environ.get(self.password_env, "")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MNOGOBASE_", env_nested_delimiter="__", extra="ignore"
    )

    device: Literal["auto", "cuda", "mps", "cpu"] = "auto"
    data_dir: Path = Path(".mnogobase")
    logs_dir: Path = Path("logs")
    runs_dir: Path = Path("runs")
    llm: LLMSettings = Field(default_factory=LLMSettings)
    embedder: EmbedderSettings = Field(default_factory=EmbedderSettings)
    sparse: SparseSettings = Field(default_factory=SparseSettings)
    parsing: ParsingSettings = Field(default_factory=ParsingSettings)
    chunking: ChunkingSettings = Field(default_factory=ChunkingSettings)
    extract: ExtractSettings = Field(default_factory=ExtractSettings)
    resolve: ResolveSettings = Field(default_factory=ResolveSettings)
    wiki: WikiSettings = Field(default_factory=WikiSettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    graph: GraphSettings = Field(default_factory=GraphSettings)
    qdrant: QdrantSettings = Field(default_factory=QdrantSettings)
    neo4j: Neo4jSettings = Field(default_factory=Neo4jSettings)

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        # priority: explicit init > MNOGOBASE_* env > config.yaml > defaults
        return (init_settings, env_settings, YamlConfigSettingsSource(settings_cls), file_secret_settings)


def load_settings(config_path: Path | None = None) -> Settings:
    """Load `.env` secrets from the CWD, then settings from YAML + env."""
    load_dotenv(Path.cwd() / ".env", override=False)
    path = Path(config_path or os.environ.get("MNOGOBASE_CONFIG", "config.yaml"))

    class _FileSettings(Settings):
        model_config = {**Settings.model_config, "yaml_file": path}

    return _FileSettings()
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_config.py -v`
Expected: 4 passed.

- [ ] **Step 6: Create `config.yaml`, `.env.example`, `.env`, Docker files and test package markers**

`config.yaml`:

```yaml
device: auto                         # auto | cuda | mps | cpu
data_dir: .mnogobase
logs_dir: logs
runs_dir: runs

llm:
  base_url: https://codex.sale/v1    # local: http://localhost:11434/v1
  model: gpt-6-luna                  # local: gemma4:26b-a4b
  api_key_env: LLM_API_KEY
  concurrency: 4
  temperature: 0
  timeout_s: 120
  overrides: {extract: null, resolve: null, wiki: null, answer: null}

embedder:
  provider: ollama
  base_url: http://localhost:11434
  model: embeddinggemma-2:740m
  dim: 768
  batch_size: 32
  doc_template: "title: {title} | text: {text}"
  query_template: "task: search result | query: {query}"
  # Fallback if EmbeddingGemma 2 is unavailable (requires `mnogobase reindex` after switching):
  # model: qwen3-embedding:0.6b
  # dim: 1024
  # doc_template: "{text}"
  # query_template: "Instruct: Given a question, retrieve passages that answer it\nQuery: {query}"

sparse:
  model: Qdrant/bm25

parsing:
  ocr: true
  extensions: [pdf, docx, pptx, xlsx, html, htm, md, adoc, csv, txt]

chunking:
  tokenizer: google/embeddinggemma-2
  max_tokens: 512

extract:
  entity_types: [Person, Organization, Concept, Method, Technology, Dataset, Work, Event, Location, Other]
  max_failed_ratio: 0.2

resolve:
  auto_merge: 0.92
  llm_check: 0.80
  max_descriptions: 5

wiki:
  dir: wiki
  language: en
  min_mentions: 2
  evidence_k: 12

retrieval:
  k: 8
  context_tokens: 6000
  budget: {rag: 0.4, wiki: 0.3, graph: 0.3}

graph:
  hops: 2
  max_relations: 30
  seeds: 5
  seed_threshold: 0.3

qdrant:
  url: http://localhost:6333
  prefix: mb_

neo4j:
  uri: bolt://localhost:7687
  user: neo4j
  password_env: NEO4J_PASSWORD
```

`.env.example`:

```dotenv
# Copy to .env (never commit .env)
LLM_API_KEY=
NEO4J_PASSWORD=mnogobase-dev
```

Create `.env` (git-ignored) with `LLM_API_KEY=<the key the user gave in chat>` and `NEO4J_PASSWORD=mnogobase-dev`. Confirm with `git check-ignore .env` (prints `.env`).

`docker-compose.yml`:

```yaml
services:
  qdrant:
    image: qdrant/qdrant:v1.19.2
    ports: ["6333:6333", "6334:6334"]
    volumes: ["qdrant_data:/qdrant/storage"]
    restart: unless-stopped

  neo4j:
    image: neo4j:5.26-community
    ports: ["7474:7474", "7687:7687"]
    environment:
      NEO4J_AUTH: neo4j/${NEO4J_PASSWORD:-mnogobase-dev}
      NEO4J_PLUGINS: '["apoc", "graph-data-science"]'
      NEO4J_dbms_security_procedures_unrestricted: apoc.*,gds.*
      NEO4J_server_memory_heap_max__size: 2G
    volumes: ["neo4j_data:/data"]
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://localhost:7474 || exit 1"]
      interval: 10s
      retries: 12
    restart: unless-stopped

volumes:
  qdrant_data:
  neo4j_data:
```

`docker-compose.cuda.yml` (first verify the tag exists: `curl -s 'https://hub.docker.com/v2/repositories/qdrant/qdrant/tags?page_size=5&name=gpu-nvidia'`; use the newest `v1.19.*-gpu-nvidia` it lists):

```yaml
# docker compose -f docker-compose.yml -f docker-compose.cuda.yml up -d   (Linux + NVIDIA Container Toolkit)
services:
  qdrant:
    image: qdrant/qdrant:v1.19.2-gpu-nvidia
    environment:
      QDRANT__GPU__INDEXING: "1"
    deploy:
      resources:
        reservations:
          devices: [{driver: nvidia, count: all, capabilities: [gpu]}]

  ollama:
    image: ollama/ollama:latest
    ports: ["11434:11434"]
    environment:
      OLLAMA_NUM_PARALLEL: "4"
    volumes: ["ollama_data:/root/.ollama"]
    deploy:
      resources:
        reservations:
          devices: [{driver: nvidia, count: all, capabilities: [gpu]}]

volumes:
  ollama_data:
```

Create empty `tests/__init__.py`, `tests/unit/__init__.py`, `tests/integration/__init__.py`, `tests/e2e/__init__.py`.

- [ ] **Step 7: Validate compose files and lint**

Run: `docker compose config -q && docker compose -f docker-compose.yml -f docker-compose.cuda.yml config -q && uv run ruff check . && uv run ruff format --check .`
Expected: no output, exit 0 (run `uv run ruff format .` first if format check fails).

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml uv.lock config.yaml .env.example docker-compose.yml docker-compose.cuda.yml src tests
git commit -m "feat: project scaffold, settings loader, docker services"
```

---

### Task 2: Deterministic IDs and domain models

**Files:**
- Create: `src/mnogobase/ids.py`, `src/mnogobase/models.py`
- Test: `tests/unit/test_ids.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `ids.file_doc_id(path: Path) -> str`, `ids.chunk_id(doc_id: str, idx: int) -> str`, `ids.normalize_name(name: str) -> str`, `ids.entity_id(entity_type: str, name: str) -> str`, `ids.slugify(name: str) -> str`, `ids.point_id(key: str) -> str`.
  - `models`: `Modality`, `EmbedInput`, `DocumentRecord`, `ChunkRecord`, `ExtractedEntity`, `ExtractedRelation`, `ExtractionResult`, `EntityRecord`, `RelationView`, `EntityContext`, `ChunkView`, `WikiPageRecord`, `PageIndexRow`, `SearchHit`, `ContextItem`, `Source`, `Answer` (fields exactly as in Step 3).

- [ ] **Step 1: Write the failing test**

`tests/unit/test_ids.py`:

```python
import uuid

from mnogobase import ids


def test_normalize_name():
    assert ids.normalize_name("  Self-Attention!! ") == "self attention"
    assert ids.normalize_name("Механизм  внимания") == "механизм внимания"
    assert ids.normalize_name("C++") == "c++"
    assert ids.normalize_name("C#") == "c#"
    assert ids.normalize_name("ＴＲＡＮＳＦＯＲＭＥＲ") == "transformer"  # NFKC full-width


def test_entity_id_is_stable_case_insensitive_and_type_sensitive():
    a = ids.entity_id("Method", "Self-Attention")
    assert a == ids.entity_id("method", "self attention")
    assert a != ids.entity_id("Concept", "Self-Attention")
    assert len(a) == 16


def test_chunk_id_and_point_id():
    assert ids.chunk_id("abcdef0123456789", 7) == "abcdef0123456789:00007"
    p = ids.point_id("abcdef0123456789:00007")
    assert uuid.UUID(p)
    assert p == ids.point_id("abcdef0123456789:00007")
    assert p != ids.point_id("abcdef0123456789:00008")


def test_slugify():
    assert ids.slugify("Retrieval-Augmented Generation") == "retrieval-augmented-generation"
    assert ids.slugify("!!!") == "untitled"


def test_file_doc_id_depends_on_content_only(tmp_path):
    a = tmp_path / "a.md"
    b = tmp_path / "sub" / "b.md"
    b.parent.mkdir()
    a.write_text("same", encoding="utf-8")
    b.write_text("same", encoding="utf-8")
    assert ids.file_doc_id(a) == ids.file_doc_id(b)
    b.write_text("different", encoding="utf-8")
    assert ids.file_doc_id(a) != ids.file_doc_id(b)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/unit/test_ids.py -v`
Expected: FAIL with `ImportError: cannot import name 'ids'`.

- [ ] **Step 3: Implement `ids.py` and `models.py`**

`src/mnogobase/ids.py`:

```python
from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from pathlib import Path

NAMESPACE = uuid.UUID("5b0c9a8e-3f61-4d2a-9b7e-0c1d2e3f4a5b")
# keep + and # so that "C++" / "C#" do not collapse into "c"
_SEPARATORS = re.compile(r"[^\w+#]+|_+", re.UNICODE)


def file_doc_id(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()[:16]


def chunk_id(doc_id: str, idx: int) -> str:
    return f"{doc_id}:{idx:05d}"


def normalize_name(name: str) -> str:
    text = unicodedata.normalize("NFKC", name).casefold()
    return " ".join(_SEPARATORS.sub(" ", text).split())


def entity_id(entity_type: str, name: str) -> str:
    key = f"{entity_type.casefold()}|{normalize_name(name)}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def slugify(name: str) -> str:
    return normalize_name(name).replace(" ", "-") or "untitled"


def point_id(key: str) -> str:
    return str(uuid.uuid5(NAMESPACE, key))
```

Check `ids.slugify("!!!")`: `normalize_name("!!!")` → `""` → `"untitled"`. Check `normalize_name("Retrieval-Augmented Generation")` → `"retrieval augmented generation"` → slug `"retrieval-augmented-generation"`.

`src/mnogobase/models.py`:

```python
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Modality = Literal["text", "image", "audio"]
ContextKind = Literal["chunk", "wiki", "relation", "entity"]


class EmbedInput(BaseModel):
    """One thing to embed. Only `text` is implemented; image/audio are reserved."""

    modality: Modality = "text"
    text: str | None = None
    path: Path | None = None
    title: str | None = None


class DocumentRecord(BaseModel):
    doc_id: str
    path: str
    title: str
    mime: str
    n_pages: int | None = None


class ChunkRecord(BaseModel):
    chunk_id: str
    doc_id: str
    idx: int
    text: str
    context_text: str
    headings: list[str] = Field(default_factory=list)
    page_start: int | None = None
    page_end: int | None = None
    n_tokens: int = 0
    modality: Modality = "text"
    path: str = ""


# --- LLM extraction schemas (sent to the model as strict JSON Schema) ---

class ExtractedEntity(BaseModel):
    name: str
    type: str
    description: str
    aliases: list[str]


class ExtractedRelation(BaseModel):
    source: str
    target: str
    predicate: str
    description: str
    strength: int


class ExtractionResult(BaseModel):
    entities: list[ExtractedEntity]
    relations: list[ExtractedRelation]


# --- graph / wiki records ---

class EntityRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    entity_id: str
    name: str
    type: str
    aliases: list[str] = Field(default_factory=list)
    description: str = ""
    descriptions: list[str] = Field(default_factory=list)
    mention_count: int = 0


class RelationView(BaseModel):
    model_config = ConfigDict(extra="ignore")

    src_id: str
    src_name: str
    predicate: str
    dst_id: str
    dst_name: str
    description: str = ""
    weight: float = 0.0
    evidence: list[str] = Field(default_factory=list)


class EntityContext(BaseModel):
    entity: EntityRecord
    relations: list[RelationView] = Field(default_factory=list)


class ChunkView(BaseModel):
    chunk_id: str
    doc_id: str
    text: str
    path: str | None = None
    page: int | None = None
    headings: list[str] = Field(default_factory=list)


class WikiPageRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    page_id: str
    slug: str
    title: str
    path: str  # relative to the wiki dir, e.g. "entities/transformer.md"
    content_hash: str = ""
    version: int = 1


class PageIndexRow(BaseModel):
    page_id: str
    slug: str
    title: str
    path: str
    entity_type: str
    aliases: list[str] = Field(default_factory=list)


# --- retrieval ---

class SearchHit(BaseModel):
    key: str
    score: float
    payload: dict[str, Any] = Field(default_factory=dict)


class ContextItem(BaseModel):
    kind: ContextKind
    ref: str
    text: str
    path: str | None = None
    page: int | None = None
    score: float = 0.0


class Source(BaseModel):
    n: int
    kind: ContextKind
    ref: str
    path: str | None = None
    page: int | None = None
    snippet: str = ""
    cited: bool = False


class Answer(BaseModel):
    question: str
    mode: str
    text: str
    sources: list[Source] = Field(default_factory=list)
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    context_tokens: int = 0
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/unit/test_ids.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mnogobase/ids.py src/mnogobase/models.py tests/unit/test_ids.py
git commit -m "feat: deterministic ids and domain models"
```

---

### Task 3: Logging and device detection

**Files:**
- Create: `src/mnogobase/log.py`, `src/mnogobase/device.py`
- Test: `tests/unit/test_log.py`, `tests/unit/test_device.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `log.configure_logging(logs_dir: Path, level: str = "INFO", console: bool = True) -> None`; `log.get_logger(name: str | None = None)`; `log.new_run_id() -> str` (binds `run_id` into structlog contextvars); `log.log_stage(logger, stage: str, **fields)` context manager emitting `stage_done`/`stage_failed` with `duration_ms`; `device.detect_device(preference: str = "auto") -> Literal["cuda", "mps", "cpu"]`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_log.py`:

```python
import json
import logging

import pytest
import structlog

from mnogobase.log import configure_logging, get_logger, log_stage, new_run_id


def _read(logs_dir):
    for h in logging.getLogger().handlers:
        h.flush()
    lines = (logs_dir / "mnogobase.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def test_jsonl_contains_context_fields(tmp_path):
    configure_logging(tmp_path, console=False)
    run_id = new_run_id()
    get_logger("t").info("hello", doc_id="d1", text="привет")
    rec = _read(tmp_path)[-1]
    assert rec["event"] == "hello"
    assert rec["run_id"] == run_id
    assert rec["doc_id"] == "d1"
    assert rec["text"] == "привет"
    assert rec["level"] == "info"
    assert "ts" in rec
    structlog.contextvars.clear_contextvars()


def test_log_stage_success_and_failure(tmp_path):
    configure_logging(tmp_path, console=False)
    log = get_logger("t")
    with log_stage(log, "parse", doc_id="d1"):
        pass
    with pytest.raises(ValueError), log_stage(log, "chunk", doc_id="d1"):
        raise ValueError("boom")
    done, failed = _read(tmp_path)[-2:]
    assert done["event"] == "stage_done" and done["stage"] == "parse"
    assert isinstance(done["duration_ms"], int)
    assert failed["event"] == "stage_failed" and failed["stage"] == "chunk"
    assert "boom" in failed["error"]
```

`tests/unit/test_device.py`:

```python
from mnogobase import device


def test_explicit_preference_wins(monkeypatch):
    monkeypatch.setattr(device, "_cuda_available", lambda: True)
    assert device.detect_device("cpu") == "cpu"


def test_auto_prefers_cuda_then_mps(monkeypatch):
    monkeypatch.setattr(device, "_cuda_available", lambda: True)
    monkeypatch.setattr(device, "_mps_available", lambda: True)
    assert device.detect_device("auto") == "cuda"
    monkeypatch.setattr(device, "_cuda_available", lambda: False)
    assert device.detect_device("auto") == "mps"
    monkeypatch.setattr(device, "_mps_available", lambda: False)
    assert device.detect_device("auto") == "cpu"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_log.py tests/unit/test_device.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mnogobase.log'`.

- [ ] **Step 3: Implement `log.py` and `device.py`**

`src/mnogobase/log.py`:

```python
from __future__ import annotations

import logging
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog

_SHARED = [
    structlog.contextvars.merge_contextvars,
    structlog.processors.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", key="ts"),
]


def configure_logging(logs_dir: Path, level: str = "INFO", console: bool = True) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    structlog.configure(
        processors=[*_SHARED, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    file_handler = RotatingFileHandler(
        logs_dir / "mnogobase.jsonl", maxBytes=10_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.dict_tracebacks,
                structlog.processors.JSONRenderer(ensure_ascii=False),
            ],
            foreign_pre_chain=_SHARED,
        )
    )
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.addHandler(file_handler)
    if console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.dev.ConsoleRenderer(),
                ],
                foreign_pre_chain=_SHARED,
            )
        )
        console_handler.setLevel(logging.WARNING)  # Rich progress owns the terminal
        root.addHandler(console_handler)
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "neo4j", "urllib3", "docling", "filelock"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None):
    return structlog.get_logger(name)


def new_run_id() -> str:
    run_id = uuid.uuid4().hex[:12]
    structlog.contextvars.bind_contextvars(run_id=run_id)
    return run_id


@contextmanager
def log_stage(logger, stage: str, **fields) -> Iterator[None]:
    start = time.perf_counter()
    try:
        yield
    except Exception as exc:
        logger.error(
            "stage_failed",
            stage=stage,
            duration_ms=int((time.perf_counter() - start) * 1000),
            error_type=type(exc).__name__,
            error=str(exc),
            exc_info=True,
            **fields,
        )
        raise
    logger.info(
        "stage_done", stage=stage, duration_ms=int((time.perf_counter() - start) * 1000), **fields
    )
```

`src/mnogobase/device.py`:

```python
from __future__ import annotations

import platform
import shutil
from typing import Literal

Device = Literal["cuda", "mps", "cpu"]


def _cuda_available() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available())
    except ImportError:
        return shutil.which("nvidia-smi") is not None


def _mps_available() -> bool:
    try:
        import torch

        return bool(torch.backends.mps.is_available())
    except ImportError:
        return platform.system() == "Darwin" and platform.machine() == "arm64"


def detect_device(preference: str = "auto") -> Device:
    if preference in ("cuda", "mps", "cpu"):
        return preference  # type: ignore[return-value]
    if _cuda_available():
        return "cuda"
    if _mps_available():
        return "mps"
    return "cpu"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_log.py tests/unit/test_device.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mnogobase/log.py src/mnogobase/device.py tests/unit/test_log.py tests/unit/test_device.py
git commit -m "feat: structured JSONL logging and device detection"
```

---

### Task 4: SQLite registry

**Files:**
- Create: `src/mnogobase/registry.py`
- Test: `tests/unit/test_registry.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `registry.STAGES = ("parse", "chunk", "embed", "extract", "graph")`; `FileRow(path, doc_id, size, mtime, status)`; `class Registry(db_path: Path)` with methods:
  `close()`, `get_file(path) -> FileRow | None`, `upsert_file(path, doc_id, size, mtime, status)`, `set_file_status(path, status)`, `files() -> list[FileRow]`, `failed_paths() -> list[str]`, `paths_for_doc(doc_id) -> list[str]`, `doc_ids() -> list[str]` (docs with status `done`),
  `stage_status(doc_id, stage) -> str` (`"pending"` when absent), `set_stage(doc_id, stage, status, error=None)`, `pending_stages(doc_id) -> list[str]`, `reset_running() -> int`, `stage_errors() -> list[tuple[str, str, str]]`, `stage_summary() -> dict[str, dict[str, int]]`, `clear_doc(doc_id)`,
  `set_chunk_extract(chunk_id, status, error=None)`, `chunk_extract_counts(doc_id) -> dict[str, int]`,
  `get_extraction(chunk_id, prompt_version, model) -> str | None`, `put_extraction(chunk_id, prompt_version, model, result_json)`,
  `mark_dirty(entity_ids: Iterable[str])`, `dirty() -> list[str]`, `clear_dirty(entity_ids: Iterable[str])`,
  `get_meta(key) -> str | None`, `set_meta(key, value)`, `wipe()`;
  `ingest_lock(data_dir: Path) -> filelock.FileLock` (timeout 0).

- [ ] **Step 1: Write the failing test**

`tests/unit/test_registry.py`:

```python
import filelock
import pytest

from mnogobase.registry import STAGES, Registry, ingest_lock


@pytest.fixture
def reg(tmp_path):
    r = Registry(tmp_path / "state.db")
    yield r
    r.close()


def test_stage_lifecycle_and_attempts(reg):
    assert reg.pending_stages("d1") == list(STAGES)
    reg.set_stage("d1", "parse", "running")
    reg.set_stage("d1", "parse", "failed", error="boom")
    reg.set_stage("d1", "parse", "running")
    reg.set_stage("d1", "parse", "done")
    assert reg.stage_status("d1", "parse") == "done"
    assert reg.pending_stages("d1") == list(STAGES[1:])
    row = reg._db.execute("SELECT attempts FROM stages WHERE doc_id='d1'").fetchone()
    assert row[0] == 2


def test_reset_running_and_errors(reg):
    reg.set_stage("d1", "chunk", "running")
    reg.set_stage("d2", "embed", "failed", error="ConnectError: down")
    assert reg.reset_running() == 1
    assert reg.stage_status("d1", "chunk") == "pending"
    assert reg.stage_errors() == [("d2", "embed", "ConnectError: down")]
    assert reg.stage_summary()["embed"] == {"failed": 1}


def test_files_and_paths(reg):
    reg.upsert_file("/a.md", "d1", 10, 1.0, "done")
    reg.upsert_file("/b.md", "d1", 10, 1.0, "done")
    reg.upsert_file("/c.md", "d2", 5, 2.0, "failed")
    assert reg.get_file("/a.md").doc_id == "d1"
    assert sorted(reg.paths_for_doc("d1")) == ["/a.md", "/b.md"]
    assert reg.doc_ids() == ["d1"]
    assert reg.failed_paths() == ["/c.md"]
    reg.set_file_status("/c.md", "done")
    assert reg.failed_paths() == []


def test_clear_doc_removes_stage_and_chunk_rows(reg):
    reg.set_stage("d1", "parse", "done")
    reg.set_chunk_extract("d1:00000", "failed", "x")
    reg.set_chunk_extract("d2:00000", "done")
    reg.put_extraction("d1:00000", "v1", "m", "{}")
    reg.clear_doc("d1")
    assert reg.pending_stages("d1") == list(STAGES)
    assert reg.chunk_extract_counts("d1") == {}
    assert reg.chunk_extract_counts("d2") == {"done": 1}
    assert reg.get_extraction("d1:00000", "v1", "m") is None


def test_extraction_cache_dirty_meta(reg):
    reg.put_extraction("c1", "v1", "m", '{"a": 1}')
    reg.put_extraction("c1", "v1", "m", '{"a": 2}')
    assert reg.get_extraction("c1", "v1", "m") == '{"a": 2}'
    assert reg.get_extraction("c1", "v2", "m") is None
    reg.mark_dirty(["e1", "e2", "e1"])
    assert sorted(reg.dirty()) == ["e1", "e2"]
    reg.clear_dirty(["e1"])
    assert reg.dirty() == ["e2"]
    reg.set_meta("embedder", "ollama:x:768")
    assert reg.get_meta("embedder") == "ollama:x:768"
    reg.wipe()
    assert reg.dirty() == [] and reg.get_meta("embedder") is None


def test_ingest_lock_is_exclusive(tmp_path):
    with ingest_lock(tmp_path), pytest.raises(filelock.Timeout):
        ingest_lock(tmp_path).acquire()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/unit/test_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mnogobase.registry'`.

- [ ] **Step 3: Implement `registry.py`**

```python
from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from filelock import FileLock

STAGES: tuple[str, ...] = ("parse", "chunk", "embed", "extract", "graph")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
    path TEXT PRIMARY KEY, doc_id TEXT NOT NULL, size INTEGER, mtime REAL,
    status TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS files_doc ON files(doc_id);
CREATE TABLE IF NOT EXISTS stages(
    doc_id TEXT, stage TEXT, status TEXT, attempts INTEGER DEFAULT 0, error TEXT,
    updated_at TEXT, PRIMARY KEY(doc_id, stage));
CREATE TABLE IF NOT EXISTS chunk_extract(
    chunk_id TEXT PRIMARY KEY, status TEXT, attempts INTEGER DEFAULT 0, error TEXT);
CREATE TABLE IF NOT EXISTS extraction_cache(
    chunk_id TEXT, prompt_version TEXT, model TEXT, result_json TEXT,
    PRIMARY KEY(chunk_id, prompt_version, model));
CREATE TABLE IF NOT EXISTS dirty_entities(entity_id TEXT PRIMARY KEY, marked_at TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class FileRow:
    path: str
    doc_id: str
    size: int
    mtime: float
    status: str


class Registry:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    # ---- files ----
    def get_file(self, path: str) -> FileRow | None:
        row = self._db.execute(
            "SELECT path, doc_id, size, mtime, status FROM files WHERE path=?", (path,)
        ).fetchone()
        return FileRow(*row) if row else None

    def upsert_file(self, path: str, doc_id: str, size: int, mtime: float, status: str) -> None:
        self._db.execute(
            "INSERT INTO files(path, doc_id, size, mtime, status, updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET doc_id=excluded.doc_id, size=excluded.size, "
            "mtime=excluded.mtime, status=excluded.status, updated_at=excluded.updated_at",
            (path, doc_id, size, mtime, status, _now()),
        )

    def set_file_status(self, path: str, status: str) -> None:
        self._db.execute(
            "UPDATE files SET status=?, updated_at=? WHERE path=?", (status, _now(), path)
        )

    def files(self) -> list[FileRow]:
        rows = self._db.execute(
            "SELECT path, doc_id, size, mtime, status FROM files ORDER BY path"
        ).fetchall()
        return [FileRow(*r) for r in rows]

    def failed_paths(self) -> list[str]:
        rows = self._db.execute("SELECT path FROM files WHERE status='failed' ORDER BY path")
        return [r[0] for r in rows]

    def paths_for_doc(self, doc_id: str) -> list[str]:
        rows = self._db.execute("SELECT path FROM files WHERE doc_id=? ORDER BY path", (doc_id,))
        return [r[0] for r in rows]

    def doc_ids(self) -> list[str]:
        rows = self._db.execute(
            "SELECT DISTINCT doc_id FROM files WHERE status='done' ORDER BY doc_id"
        )
        return [r[0] for r in rows]

    # ---- stages ----
    def stage_status(self, doc_id: str, stage: str) -> str:
        row = self._db.execute(
            "SELECT status FROM stages WHERE doc_id=? AND stage=?", (doc_id, stage)
        ).fetchone()
        return row[0] if row else "pending"

    def set_stage(self, doc_id: str, stage: str, status: str, error: str | None = None) -> None:
        started = 1 if status == "running" else 0
        self._db.execute(
            "INSERT INTO stages(doc_id, stage, status, attempts, error, updated_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(doc_id, stage) DO UPDATE SET "
            "status=excluded.status, error=excluded.error, updated_at=excluded.updated_at, "
            "attempts=stages.attempts + ?",
            (doc_id, stage, status, started, error, _now(), started),
        )

    def pending_stages(self, doc_id: str) -> list[str]:
        return [s for s in STAGES if self.stage_status(doc_id, s) != "done"]

    def reset_running(self) -> int:
        cur = self._db.execute("UPDATE stages SET status='pending' WHERE status='running'")
        return cur.rowcount

    def stage_errors(self) -> list[tuple[str, str, str]]:
        rows = self._db.execute(
            "SELECT doc_id, stage, error FROM stages WHERE status='failed' ORDER BY doc_id, stage"
        )
        return [tuple(r) for r in rows]

    def stage_summary(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        rows = self._db.execute("SELECT stage, status, COUNT(*) FROM stages GROUP BY stage, status")
        for stage, status, n in rows:
            out.setdefault(stage, {})[status] = n
        return out

    def clear_doc(self, doc_id: str) -> None:
        prefix = f"{doc_id}:%"
        self._db.execute("DELETE FROM stages WHERE doc_id=?", (doc_id,))
        self._db.execute("DELETE FROM chunk_extract WHERE chunk_id LIKE ?", (prefix,))
        self._db.execute("DELETE FROM extraction_cache WHERE chunk_id LIKE ?", (prefix,))

    # ---- per-chunk extraction ----
    def set_chunk_extract(self, chunk_id: str, status: str, error: str | None = None) -> None:
        self._db.execute(
            "INSERT INTO chunk_extract(chunk_id, status, attempts, error) VALUES(?,?,1,?) "
            "ON CONFLICT(chunk_id) DO UPDATE SET status=excluded.status, error=excluded.error, "
            "attempts=chunk_extract.attempts + 1",
            (chunk_id, status, error),
        )

    def chunk_extract_counts(self, doc_id: str) -> dict[str, int]:
        rows = self._db.execute(
            "SELECT status, COUNT(*) FROM chunk_extract WHERE chunk_id LIKE ? GROUP BY status",
            (f"{doc_id}:%",),
        )
        return {status: n for status, n in rows}

    def get_extraction(self, chunk_id: str, prompt_version: str, model: str) -> str | None:
        row = self._db.execute(
            "SELECT result_json FROM extraction_cache "
            "WHERE chunk_id=? AND prompt_version=? AND model=?",
            (chunk_id, prompt_version, model),
        ).fetchone()
        return row[0] if row else None

    def put_extraction(self, chunk_id: str, prompt_version: str, model: str, result_json: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO extraction_cache(chunk_id, prompt_version, model, result_json) "
            "VALUES(?,?,?,?)",
            (chunk_id, prompt_version, model, result_json),
        )

    # ---- dirty entities ----
    def mark_dirty(self, entity_ids: Iterable[str]) -> None:
        now = _now()
        self._db.executemany(
            "INSERT OR REPLACE INTO dirty_entities(entity_id, marked_at) VALUES(?, ?)",
            [(e, now) for e in set(entity_ids)],
        )

    def dirty(self) -> list[str]:
        rows = self._db.execute("SELECT entity_id FROM dirty_entities ORDER BY entity_id")
        return [r[0] for r in rows]

    def clear_dirty(self, entity_ids: Iterable[str]) -> None:
        self._db.executemany(
            "DELETE FROM dirty_entities WHERE entity_id=?", [(e,) for e in set(entity_ids)]
        )

    # ---- meta ----
    def get_meta(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))

    def wipe(self) -> None:
        for table in ("files", "stages", "chunk_extract", "extraction_cache", "dirty_entities", "meta"):
            self._db.execute(f"DELETE FROM {table}")


def ingest_lock(data_dir: Path) -> FileLock:
    data_dir.mkdir(parents=True, exist_ok=True)
    return FileLock(data_dir / "ingest.lock", timeout=0)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/unit/test_registry.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mnogobase/registry.py tests/unit/test_registry.py
git commit -m "feat: sqlite registry for files, stages, extraction cache, dirty entities"
```

---
### Task 5: LLM client, prompt templates, test fakes for the LLM

**Files:**
- Create: `src/mnogobase/llm/__init__.py` (empty), `src/mnogobase/llm/client.py`, `src/mnogobase/llm/templates.py`
- Create: `src/mnogobase/llm/prompts/extract.md`, `resolve_same.md`, `resolve_summarize.md`, `wiki_page.md`, `answer.md`
- Create: `tests/fakes.py`
- Test: `tests/unit/test_llm_client.py`, `tests/unit/test_templates.py`

**Interfaces:**
- Consumes: `config.LLMSettings` (Task 1), `log.get_logger` (Task 3).
- Produces:
  - `llm.client.Message = dict[str, str]`; `Usage(tokens_in: int, tokens_out: int)`; `StructuredOutputError(RuntimeError)`.
  - `LLMClient` protocol: `usage: Usage`; `model_for(task: str) -> str`; `async complete(messages: list[Message], *, task: str) -> str`; `async structured(messages: list[Message], schema: type[T], *, task: str, max_repairs: int = 2) -> T`.
  - `OpenAICompatLLM(settings: LLMSettings, http_client: httpx.AsyncClient | None = None, retry_wait=wait_exponential(multiplier=1, max=30))` implementing `LLMClient`.
  - `strict_json_schema(model: type[BaseModel]) -> dict`, `clean_json(text: str) -> str`, `strip_think(text: str) -> str`.
  - `llm.templates.render(name: str, **values) -> str` reading `llm/prompts/<name>.md` with `string.Template` placeholders.
  - `tests.fakes.FakeLLM(handler: Callable[[str, str], str])` (handler gets `(task, joined_prompt)`), `FakeLLM.calls: list[tuple[str, str]]`, `FakeLLM.calls_for(task) -> list[str]`; `tests.fakes.scripted_llm_handler(task, prompt) -> str`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_llm_client.py`:

```python
import json

import httpx
import pytest
from pydantic import BaseModel
from tenacity import wait_none

from mnogobase.config import LLMSettings
from mnogobase.llm.client import (
    OpenAICompatLLM,
    StructuredOutputError,
    clean_json,
    strict_json_schema,
)


class Item(BaseModel):
    title: str
    tags: list[str] = []


class Box(BaseModel):
    items: list[Item]
    count: int


def _completion(content: str) -> dict:
    return {
        "id": "c1", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
    }


def make_llm(replies, settings: LLMSettings | None = None):
    requests: list[dict] = []
    queue = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        reply = queue.pop(0)
        if isinstance(reply, int):
            return httpx.Response(reply, json={"error": {"message": "server error"}})
        return httpx.Response(200, json=_completion(reply))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    llm = OpenAICompatLLM(
        settings or LLMSettings(base_url="http://test/v1", model="base"),
        http_client=client,
        retry_wait=wait_none(),
    )
    return llm, requests


def test_strict_json_schema_is_strict_and_keeps_title_property():
    schema = strict_json_schema(Box)
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["items", "count"]
    item = schema["$defs"]["Item"]
    assert item["additionalProperties"] is False
    assert item["required"] == ["title", "tags"]
    assert "title" in item["properties"]          # a property literally named "title" survives
    assert "title" not in item                    # the schema keyword "title" is removed
    assert "default" not in item["properties"]["tags"]


def test_clean_json():
    raw = '<think>let me think</think>\n```json\n{"a": 1}\n```'
    assert clean_json(raw) == '{"a": 1}'
    assert clean_json('Sure! {"a": 2} hope this helps') == '{"a": 2}'


async def test_structured_strips_fences_and_think():
    llm, requests = make_llm(['<think>x</think>```json\n{"items": [], "count": 0}\n```'])
    box = await llm.structured([{"role": "user", "content": "go"}], Box, task="extract")
    assert box.count == 0
    rf = requests[0]["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["schema"]["additionalProperties"] is False


async def test_structured_repairs_invalid_json():
    llm, requests = make_llm(['{"items": [], "count": "many"}', '{"items": [], "count": 2}'])
    box = await llm.structured([{"role": "user", "content": "go"}], Box, task="extract")
    assert box.count == 2
    assert len(requests) == 2
    assert "not valid JSON" in requests[1]["messages"][-1]["content"]


async def test_structured_gives_up_after_repairs():
    llm, requests = make_llm(["nope", "nope", "nope"])
    with pytest.raises(StructuredOutputError):
        await llm.structured([{"role": "user", "content": "go"}], Box, task="extract")
    assert len(requests) == 3


async def test_retries_server_errors():
    llm, requests = make_llm([500, '{"items": [], "count": 1}'])
    box = await llm.structured([{"role": "user", "content": "go"}], Box, task="extract")
    assert box.count == 1
    assert len(requests) == 2


async def test_model_override_usage_and_think_stripping():
    settings = LLMSettings(base_url="http://test/v1", model="base", overrides={"wiki": "big"})
    llm, requests = make_llm(["<think>hidden</think>Visible text"], settings)
    text = await llm.complete([{"role": "user", "content": "hi"}], task="wiki")
    assert text == "Visible text"
    assert requests[0]["model"] == "big"
    assert (llm.usage.tokens_in, llm.usage.tokens_out) == (3, 5)
```

`tests/unit/test_templates.py`:

```python
import pytest

from mnogobase.llm.templates import render

CASES = {
    "extract": dict(entity_types="Person, Other", title="T", headings="H", text="body"),
    "resolve_same": dict(a_name="A", a_type="X", a_description="d", b_name="B", b_type="X", b_description="d"),
    "resolve_summarize": dict(name="N", type="X", descriptions="- a\n- b"),
    "wiki_page": dict(language="English", name="N", type="X", description="d", aliases="-",
                      relations="-", evidence="[id] text", example_id="id", existing=""),
    "answer": dict(question="Q?", context="[1] ctx"),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_every_placeholder_is_filled(name):
    text = render(name, **CASES[name])
    assert "$" not in text
    for value in CASES[name].values():
        assert value in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_llm_client.py tests/unit/test_templates.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mnogobase.llm'`.

- [ ] **Step 3: Implement the client**

`src/mnogobase/llm/client.py`:

```python
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

import httpx
import openai
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from mnogobase.config import LLMSettings
from mnogobase.log import get_logger

T = TypeVar("T", bound=BaseModel)
Message = dict[str, str]

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
_RETRYABLE = (
    openai.APIConnectionError,  # includes APITimeoutError
    openai.RateLimitError,
    openai.InternalServerError,
)


class StructuredOutputError(RuntimeError):
    """The model did not return JSON matching the schema after all repair attempts."""


@dataclass
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0


class LLMClient(Protocol):
    usage: Usage

    def model_for(self, task: str) -> str: ...

    async def complete(self, messages: list[Message], *, task: str) -> str: ...

    async def structured(
        self, messages: list[Message], schema: type[T], *, task: str, max_repairs: int = 2
    ) -> T: ...


def strip_think(text: str) -> str:
    return _THINK.sub("", text).strip()


def clean_json(text: str) -> str:
    text = _FENCE.sub("", strip_think(text)).strip()
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


def _strictify(node: Any) -> Any:
    if isinstance(node, list):
        return [_strictify(x) for x in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in ("default", "title"):
            continue
        if key in ("properties", "$defs"):
            out[key] = {name: _strictify(sub) for name, sub in value.items()}
        else:
            out[key] = _strictify(value)
    if out.get("type") == "object" and "properties" in out:
        out["additionalProperties"] = False
        out["required"] = list(out["properties"])
    return out


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic schema → OpenAI strict structured-output schema."""
    return _strictify(model.model_json_schema())


class OpenAICompatLLM:
    """Works with any OpenAI-compatible endpoint: OpenAI, Ollama /v1, vLLM, LM Studio, proxies."""

    def __init__(
        self,
        settings: LLMSettings,
        http_client: httpx.AsyncClient | None = None,
        retry_wait=wait_exponential(multiplier=1, max=30),
    ):
        self._settings = settings
        self._client = AsyncOpenAI(
            base_url=settings.base_url,
            api_key=settings.api_key(),
            timeout=settings.timeout_s,
            max_retries=0,  # tenacity owns retries
            http_client=http_client,
        )
        self._sem = asyncio.Semaphore(settings.concurrency)
        self._retry_wait = retry_wait
        self.usage = Usage()
        self._log = get_logger(__name__)

    def model_for(self, task: str) -> str:
        return self._settings.model_for(task)

    async def _chat(
        self, messages: list[Message], *, task: str, response_format: dict | None = None
    ) -> str:
        model = self.model_for(task)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": self._settings.temperature,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        start = time.perf_counter()
        attempts = 0
        async with self._sem:
            async for attempt in AsyncRetrying(
                retry=retry_if_exception_type(_RETRYABLE),
                stop=stop_after_attempt(5),
                wait=self._retry_wait,
                reraise=True,
            ):
                with attempt:
                    attempts += 1
                    response = await self._client.chat.completions.create(**kwargs)
        usage = response.usage
        tokens_in = usage.prompt_tokens if usage else 0
        tokens_out = usage.completion_tokens if usage else 0
        self.usage.tokens_in += tokens_in
        self.usage.tokens_out += tokens_out
        self._log.info(
            "llm_call",
            task=task,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            retries=attempts - 1,
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
        return response.choices[0].message.content or ""

    async def complete(self, messages: list[Message], *, task: str) -> str:
        return strip_think(await self._chat(messages, task=task))

    async def structured(
        self, messages: list[Message], schema: type[T], *, task: str, max_repairs: int = 2
    ) -> T:
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": schema.__name__,
                "schema": strict_json_schema(schema),
                "strict": True,
            },
        }
        history = list(messages)
        last_error = ""
        for _ in range(max_repairs + 1):
            content = await self._chat(history, task=task, response_format=response_format)
            try:
                return schema.model_validate_json(clean_json(content))
            except ValidationError as exc:  # also raised for malformed JSON
                last_error = str(exc)
                self._log.warning("llm_invalid_json", task=task, error=last_error[:300])
                history = [
                    *history,
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": "Your previous reply was not valid JSON for the schema: "
                        f"{last_error[:500]}\nReply again with only the corrected JSON.",
                    },
                ]
        raise StructuredOutputError(f"{schema.__name__}: {last_error[:500]}")
```

`src/mnogobase/llm/templates.py`:

```python
from __future__ import annotations

from importlib.resources import files
from string import Template


def render(name: str, **values: object) -> str:
    text = (files("mnogobase.llm") / "prompts" / f"{name}.md").read_text(encoding="utf-8")
    return Template(text).safe_substitute({k: str(v) for k, v in values.items()})
```

- [ ] **Step 4: Write the prompt files**

`src/mnogobase/llm/prompts/extract.md`:

```markdown
You extract a knowledge graph from one fragment of a document.

Rules:
- Extract meaningful, reusable entities: people, organizations, methods, technologies, datasets, works, events, locations and concepts. Skip generic words such as "system", "approach", "paper", "model" (unless it is a named model).
- `name`: the canonical English name with its standard spelling (e.g. "Transformer", "Andrej Karpathy", "Retrieval-Augmented Generation"). If the fragment is not in English, translate the name into English and put the original spelling into `aliases`.
- `type`: exactly one of: $entity_types.
- `description`: one or two English sentences about the entity, based only on this fragment.
- `aliases`: other names, abbreviations or original-language spellings that appear in the fragment (may be empty).
- `relations` connect two entities from your `entities` list; use exactly the same `name` values. `predicate` is a short snake_case English verb phrase ("uses", "part_of", "proposed_by", "improves_on"). `description` is one English sentence. `strength` is an integer from 1 to 10: how explicit and important the relation is in the fragment.
- Use only information present in the fragment. If nothing is worth extracting, return empty lists.

Document: $title
Section: $headings

Fragment:
"""
$text
"""
```

`src/mnogobase/llm/prompts/resolve_same.md`:

```markdown
Decide whether two knowledge-graph entities refer to the same real-world thing.

Entity A: $a_name ($a_type) — $a_description
Entity B: $b_name ($b_type) — $b_description

Answer `same: true` only for the same thing written differently: synonyms, abbreviations, translations, spelling variants. Related but different things (a method and its variant, a company and its product, a person and their work) are not the same. Give a one-sentence `reason`.
```

`src/mnogobase/llm/prompts/resolve_summarize.md`:

```markdown
Merge these descriptions of "$name" ($type) into one concise, neutral English description of at most three sentences. Keep every distinct fact and drop repetition.

$descriptions
```

`src/mnogobase/llm/prompts/wiki_page.md`:

```markdown
You maintain a personal knowledge wiki written in $language. Write the body of the wiki page about "$name" ($type).

Known facts about the entity:
$description

Aliases: $aliases

Relations in the knowledge graph:
$relations

Source fragments. Cite them with their id as a footnote marker, for example [^$example_id]:
$evidence

Current page body. It may be empty. If it is not empty, update it: keep content that is still supported, add new facts from the sources, fix contradictions, and do not drop supported information.
"""
$existing
"""

Rules:
- Start with a summary paragraph of two to four sentences, then optional "## " sections (for example "## Details", "## History", "## Usage") when there is enough material.
- End every factual sentence with at least one citation marker [^<fragment id>] that uses only the ids listed above.
- When you mention another entity from the relations list, write it as a wiki link: [[Entity Name]].
- Do not write a title line, a "## Related" section or a "## Sources" section; they are generated automatically.
- Write only what the sources support. Output only the markdown body.
```

`src/mnogobase/llm/prompts/answer.md`:

```markdown
Answer the question using only the numbered sources below. Cite sources inline with their numbers in square brackets, for example [1] or [2][3]. If the sources do not contain the answer, say that the knowledge base does not have enough information. Answer in the language of the question.

Question: $question

Sources:
$context
```

- [ ] **Step 5: Create `tests/fakes.py` (LLM part)**

```python
from __future__ import annotations

import json
import re
from collections.abc import Callable

from mnogobase.llm.client import Usage

VOCAB = {
    "transformer": ("Transformer", "Method", "Neural network architecture based entirely on attention."),
    "attention": ("Attention Mechanism", "Method", "Lets a model focus on relevant parts of its input."),
    "внимани": ("Attention Mechanism", "Method", "Lets a model focus on relevant parts of its input."),
    "softmax": ("Softmax", "Concept", "Function that turns scores into probabilities."),
    "vaswani": ("Ashish Vaswani", "Person", "Researcher, first author of the Transformer paper."),
}
_EVIDENCE_ID = re.compile(r"\[([0-9a-f]{16}:\d{5})\]")


class FakeLLM:
    """Deterministic LLMClient: `handler(task, prompt) -> reply text`."""

    def __init__(self, handler: Callable[[str, str], str]):
        self.handler = handler
        self.calls: list[tuple[str, str]] = []
        self.usage = Usage()

    def model_for(self, task: str) -> str:
        return f"fake-{task}"

    def calls_for(self, task: str) -> list[str]:
        return [prompt for t, prompt in self.calls if t == task]

    def _reply(self, task: str, messages: list[dict[str, str]]) -> str:
        prompt = "\n".join(m["content"] for m in messages)
        self.calls.append((task, prompt))
        reply = self.handler(task, prompt)
        self.usage.tokens_in += len(prompt) // 4
        self.usage.tokens_out += len(reply) // 4
        return reply

    async def complete(self, messages, *, task: str) -> str:
        return self._reply(task, messages)

    async def structured(self, messages, schema, *, task: str, max_repairs: int = 2):
        return schema.model_validate_json(self._reply(task, messages))


def scripted_llm_handler(task: str, prompt: str) -> str:
    """Keyword-driven fake used by pipeline/wiki/retrieval tests."""
    if task == "extract":
        fragment = prompt.split("Fragment:", 1)[-1].casefold()
        entities: dict[str, dict] = {}
        for key, (name, etype, description) in VOCAB.items():
            if key in fragment and name not in entities:
                aliases = ["механизм внимания"] if key == "внимани" else []
                entities[name] = {"name": name, "type": etype, "description": description,
                                  "aliases": aliases}
        names = list(entities)
        relations = [
            {"source": names[i], "target": names[i + 1], "predicate": "related_to",
             "description": f"{names[i]} relates to {names[i + 1]}.", "strength": 5}
            for i in range(len(names) - 1)
        ]
        return json.dumps({"entities": list(entities.values()), "relations": relations})
    if task == "resolve":
        if "Merge these descriptions" in prompt:
            return json.dumps({"description": "Merged description."})
        return json.dumps({"same": False, "reason": "different things"})
    if task == "wiki":
        ids = _EVIDENCE_ID.findall(prompt)
        cite = f" [^{ids[0]}]" if ids else ""
        return (f"Summary sentence.{cite} See [[Softmax]] and [[Nonexistent Thing]].\n\n"
                f"## Details\nMore text.{cite}")
    if task == "answer":
        return "The answer is supported by the sources [1]."
    raise AssertionError(f"unexpected task {task}")
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_llm_client.py tests/unit/test_templates.py -v`
Expected: 12 passed.

- [ ] **Step 7: Smoke-test against the real endpoint (manual, not committed)**

Run:
```bash
uv run python -c "
import asyncio
from pydantic import BaseModel
from mnogobase.config import load_settings
from mnogobase.llm.client import OpenAICompatLLM
from mnogobase.models import ExtractionResult
s = load_settings()
llm = OpenAICompatLLM(s.llm)
r = asyncio.run(llm.structured([{'role':'user','content':'Extract entities and relations: The Transformer was proposed by Vaswani at Google.'}], ExtractionResult, task='extract'))
print(r.model_dump_json(indent=1)); print(llm.usage)
"
```
Expected: JSON with entities such as `Transformer`, `Ashish Vaswani`/`Vaswani`, `Google`; non-zero usage. If the endpoint rejects `temperature`, set `llm.temperature` handling: drop the key when the server answers 400 mentioning `temperature` — record this in the task report instead of silently changing behaviour.

- [ ] **Step 8: Commit**

```bash
git add src/mnogobase/llm tests/fakes.py tests/unit/test_llm_client.py tests/unit/test_templates.py
git commit -m "feat: OpenAI-compatible LLM client with strict structured output and repair"
```

---

### Task 6: Dense and sparse embedders

**Files:**
- Create: `src/mnogobase/embedding/__init__.py` (empty), `src/mnogobase/embedding/base.py`, `src/mnogobase/embedding/ollama.py`, `src/mnogobase/embedding/sparse.py`
- Modify: `tests/fakes.py` (append `FakeEmbedder`, `FakeSparse`)
- Test: `tests/unit/test_embedding.py`, `tests/integration/test_sparse.py`

**Interfaces:**
- Consumes: `models.EmbedInput` (Task 2), `config.EmbedderSettings` (Task 1), `log.get_logger` (Task 3).
- Produces:
  - `embedding.base.Embedder` protocol: attributes `model_id: str`, `dim: int`; `embed_documents(items: Sequence[EmbedInput]) -> list[list[float]]`; `embed_query(query: str) -> list[float]`.
  - `embedding.base.SparseEncoder` protocol: `encode_documents(texts: Sequence[str]) -> list[qdrant_client.models.SparseVector]`; `encode_query(text: str) -> SparseVector`.
  - `format_document(item: EmbedInput, template: str) -> str` (raises `NotImplementedError` for non-text), `format_query(query: str, template: str) -> str`, `truncate_normalize(vec: Sequence[float], dim: int) -> list[float]`, `embedder_signature(embedder: Embedder) -> str` (`f"{model_id}:{dim}"`).
  - `OllamaEmbedder(settings: EmbedderSettings, client: httpx.Client | None = None, retry_wait=wait_exponential(multiplier=1, max=20))`, `model_id = f"ollama:{settings.model}"`.
  - `BM25Encoder(model: str = "Qdrant/bm25", device: str = "cpu")`.
  - `tests.fakes.FakeEmbedder(dim: int = 64)` (`model_id = "fake-embed"`), `tests.fakes.FakeSparse()`.

- [ ] **Step 1: Append the fakes to `tests/fakes.py`**

Add at the top of `tests/fakes.py` (merge with the existing imports): `import hashlib`, `import math`, `from qdrant_client import models as qm`. Append:

```python
_TOKEN = re.compile(r"\w+", re.UNICODE)


def _bucket(token: str, size: int) -> int:
    return int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16) % size


class FakeEmbedder:
    """Hashed bag-of-words: texts sharing words get high cosine similarity."""

    def __init__(self, dim: int = 64):
        self.dim = dim
        self.model_id = "fake-embed"

    def _vec(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in _TOKEN.findall(text.casefold()):
            vec[_bucket(token, self.dim)] += 1.0
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]

    def embed_documents(self, items) -> list[list[float]]:
        return [self._vec(item.text or "") for item in items]

    def embed_query(self, query: str) -> list[float]:
        return self._vec(query)


class FakeSparse:
    def _sv(self, text: str) -> qm.SparseVector:
        counts: dict[int, float] = {}
        for token in _TOKEN.findall(text.casefold()):
            idx = _bucket(token, 1_000_003)
            counts[idx] = counts.get(idx, 0.0) + 1.0
        indices = sorted(counts)
        return qm.SparseVector(indices=indices, values=[counts[i] for i in indices])

    def encode_documents(self, texts) -> list[qm.SparseVector]:
        return [self._sv(t) for t in texts]

    def encode_query(self, text: str) -> qm.SparseVector:
        return self._sv(text)
```

- [ ] **Step 2: Write the failing tests**

`tests/unit/test_embedding.py`:

```python
import json
from pathlib import Path

import httpx
import pytest
from tenacity import wait_none

from mnogobase.config import EmbedderSettings
from mnogobase.embedding.base import (
    embedder_signature,
    format_document,
    format_query,
    truncate_normalize,
)
from mnogobase.embedding.ollama import OllamaEmbedder
from mnogobase.models import EmbedInput
from tests.fakes import FakeEmbedder


def test_templates():
    s = EmbedderSettings()
    assert format_document(EmbedInput(text="hello", title="Doc"), s.doc_template) == "title: Doc | text: hello"
    assert format_document(EmbedInput(text="hello"), s.doc_template) == "title: none | text: hello"
    assert format_document(EmbedInput(text="a {b}"), "{text}") == "a {b}"
    assert format_query("q?", s.query_template) == "task: search result | query: q?"
    with pytest.raises(NotImplementedError):
        format_document(EmbedInput(modality="image", path=Path("a.png")), s.doc_template)


def test_truncate_normalize():
    assert truncate_normalize([3.0, 4.0, 12.0], 2) == pytest.approx([0.6, 0.8])
    with pytest.raises(ValueError):
        truncate_normalize([1.0], 2)


def _client(handler):
    return httpx.Client(base_url="http://ollama", transport=httpx.MockTransport(handler))


def test_ollama_batches_and_applies_templates():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(200, json={"embeddings": [[1.0, 0.0, 0.0]] * len(body["input"])})

    emb = OllamaEmbedder(EmbedderSettings(dim=2, batch_size=2), client=_client(handler))
    out = emb.embed_documents([EmbedInput(text=f"t{i}") for i in range(5)])
    assert len(out) == 5 and len(out[0]) == 2
    assert [len(b["input"]) for b in bodies] == [2, 2, 1]
    assert bodies[0]["input"][0] == "title: none | text: t0"
    assert bodies[0]["model"] == "embeddinggemma-2:740m"
    emb.embed_query("hi")
    assert bodies[-1]["input"] == ["task: search result | query: hi"]
    assert embedder_signature(emb) == "ollama:embeddinggemma-2:740m:2"


def test_ollama_retries_server_errors():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"error": "loading"})
        return httpx.Response(200, json={"embeddings": [[0.0, 1.0]]})

    emb = OllamaEmbedder(EmbedderSettings(dim=2), client=_client(handler), retry_wait=wait_none())
    assert emb.embed_query("x") == [0.0, 1.0]
    assert calls["n"] == 2


def test_ollama_rejects_too_small_vectors():
    handler = lambda request: httpx.Response(200, json={"embeddings": [[1.0]]})  # noqa: E731
    emb = OllamaEmbedder(EmbedderSettings(dim=2), client=_client(handler))
    with pytest.raises(ValueError, match="dims"):
        emb.embed_query("x")


def test_fake_embedder_similarity():
    emb = FakeEmbedder()
    a, b, c = emb.embed_documents([EmbedInput(text=t) for t in
                                   ("transformer attention", "attention transformer model", "cats")])
    dot = lambda x, y: sum(p * q for p, q in zip(x, y, strict=True))  # noqa: E731
    assert dot(a, b) > dot(a, c)
```

`tests/integration/test_sparse.py`:

```python
import pytest

from mnogobase.embedding.sparse import BM25Encoder

pytestmark = pytest.mark.integration  # downloads the BM25 model from Hugging Face


def test_bm25_query_overlaps_matching_document():
    enc = BM25Encoder()
    docs = enc.encode_documents(["the transformer uses attention", "cats are cute"])
    query = enc.encode_query("attention")
    assert docs[0].indices and docs[1].indices
    assert set(query.indices) & set(docs[0].indices)
    assert not set(query.indices) & set(docs[1].indices)
```

- [ ] **Step 3: Run the unit tests to verify they fail**

Run: `uv run pytest tests/unit/test_embedding.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mnogobase.embedding'`.

- [ ] **Step 4: Implement the embedders**

`src/mnogobase/embedding/base.py`:

```python
from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Protocol

from qdrant_client import models as qm

from mnogobase.models import EmbedInput


class Embedder(Protocol):
    model_id: str
    dim: int

    def embed_documents(self, items: Sequence[EmbedInput]) -> list[list[float]]: ...

    def embed_query(self, query: str) -> list[float]: ...


class SparseEncoder(Protocol):
    def encode_documents(self, texts: Sequence[str]) -> list[qm.SparseVector]: ...

    def encode_query(self, text: str) -> qm.SparseVector: ...


def format_document(item: EmbedInput, template: str) -> str:
    if item.modality != "text" or item.text is None:
        raise NotImplementedError(f"modality {item.modality!r} is not supported yet")
    return template.format(title=item.title or "none", text=item.text)


def format_query(query: str, template: str) -> str:
    return template.format(query=query)


def truncate_normalize(vec: Sequence[float], dim: int) -> list[float]:
    """Matryoshka truncation: keep the first `dim` values and re-normalize."""
    if len(vec) < dim:
        raise ValueError(f"model returned {len(vec)} dims, config expects {dim}")
    head = list(vec[:dim])
    norm = math.sqrt(sum(x * x for x in head)) or 1.0
    return [x / norm for x in head]


def embedder_signature(embedder: Embedder) -> str:
    return f"{embedder.model_id}:{embedder.dim}"
```

`src/mnogobase/embedding/ollama.py`:

```python
from __future__ import annotations

import time
from collections.abc import Sequence

import httpx
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

from mnogobase.config import EmbedderSettings
from mnogobase.embedding.base import format_document, format_query, truncate_normalize
from mnogobase.log import get_logger
from mnogobase.models import EmbedInput


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500


class OllamaEmbedder:
    def __init__(
        self,
        settings: EmbedderSettings,
        client: httpx.Client | None = None,
        retry_wait=wait_exponential(multiplier=1, max=20),
    ):
        self._s = settings
        self._client = client or httpx.Client(base_url=settings.base_url, timeout=120)
        self._retry_wait = retry_wait
        self.model_id = f"ollama:{settings.model}"
        self.dim = settings.dim
        self._log = get_logger(__name__)

    def _embed(self, inputs: list[str]) -> list[list[float]]:
        start = time.perf_counter()
        for attempt in Retrying(
            retry=retry_if_exception(_retryable),
            stop=stop_after_attempt(5),
            wait=self._retry_wait,
            reraise=True,
        ):
            with attempt:
                response = self._client.post(
                    "/api/embed", json={"model": self._s.model, "input": inputs, "truncate": True}
                )
                response.raise_for_status()
        vectors = response.json()["embeddings"]
        self._log.debug(
            "embed_batch", model=self._s.model, batch_size=len(inputs),
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
        return [truncate_normalize(v, self.dim) for v in vectors]

    def embed_documents(self, items: Sequence[EmbedInput]) -> list[list[float]]:
        texts = [format_document(item, self._s.doc_template) for item in items]
        out: list[list[float]] = []
        for start in range(0, len(texts), self._s.batch_size):
            out.extend(self._embed(texts[start : start + self._s.batch_size]))
        return out

    def embed_query(self, query: str) -> list[float]:
        return self._embed([format_query(query, self._s.query_template)])[0]
```

`src/mnogobase/embedding/sparse.py`:

```python
from __future__ import annotations

from collections.abc import Sequence

from qdrant_client import models as qm


def _to_qdrant(embedding) -> qm.SparseVector:
    return qm.SparseVector(indices=embedding.indices.tolist(), values=embedding.values.tolist())


class BM25Encoder:
    """BM25 term weights via fastembed; IDF is applied by Qdrant (Modifier.IDF)."""

    def __init__(self, model: str = "Qdrant/bm25", device: str = "cpu"):
        self._model_name = model
        self._cuda = device == "cuda"
        self._model = None

    def _get(self):
        if self._model is None:
            from fastembed import SparseTextEmbedding

            kwargs = {"cuda": True} if self._cuda else {}  # needs onnxruntime-gpu
            self._model = SparseTextEmbedding(self._model_name, **kwargs)
        return self._model

    def encode_documents(self, texts: Sequence[str]) -> list[qm.SparseVector]:
        return [_to_qdrant(e) for e in self._get().passage_embed(list(texts))]

    def encode_query(self, text: str) -> qm.SparseVector:
        return _to_qdrant(next(iter(self._get().query_embed(text))))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_embedding.py -v && uv run pytest -m integration tests/integration/test_sparse.py -v`
Expected: 6 passed, then 1 passed.

- [ ] **Step 6: Smoke-test the real Ollama embedder (manual)**

Run: `uv run python -c "from mnogobase.config import load_settings; from mnogobase.embedding.ollama import OllamaEmbedder; e=OllamaEmbedder(load_settings().embedder); v=e.embed_query('what is attention?'); print(len(v), round(sum(x*x for x in v),3))"`
Expected: `768 1.0` (requires `ollama pull embeddinggemma-2:740m`).

- [ ] **Step 7: Commit**

```bash
git add src/mnogobase/embedding tests/fakes.py tests/unit/test_embedding.py tests/integration/test_sparse.py
git commit -m "feat: Ollama dense embedder with MRL truncation and BM25 sparse encoder"
```

---

### Task 7: Docling parsing and hybrid chunking

**Files:**
- Create: `src/mnogobase/parsing/__init__.py` (empty), `src/mnogobase/parsing/docling_parser.py`
- Create: `src/mnogobase/chunking/__init__.py` (empty), `src/mnogobase/chunking/hybrid.py`
- Create: `tests/fixtures/attention_en.md`, `tests/fixtures/vnimanie_ru.md`
- Test: `tests/unit/test_parsing_chunking.py`

**Interfaces:**
- Consumes: `config.ParsingSettings`, `config.ChunkingSettings` (Task 1); `ids.chunk_id` (Task 2); `models.ChunkRecord` (Task 2).
- Produces:
  - `ParsedDocument(doc: DoclingDocument, title: str, mime: str, n_pages: int | None)` (dataclass).
  - `DoclingParser(settings: ParsingSettings, device: str, cache_dir: Path)` with `parse(path: Path, doc_id: str) -> ParsedDocument` (writes `cache_dir/<doc_id>.json` and `cache_dir/<doc_id>/images/`), `load(doc_id: str, path: Path) -> ParsedDocument`, `drop_cache(doc_id: str) -> None`, `cache_path(doc_id) -> Path`.
  - `Chunker(settings: ChunkingSettings)` with `chunk(parsed: ParsedDocument, doc_id: str, path: str) -> list[ChunkRecord]`.

- [ ] **Step 1: Create the fixtures**

`tests/fixtures/attention_en.md`:

```markdown
# Attention Is All You Need

## Introduction

The Transformer is a neural network architecture that relies entirely on an attention mechanism instead of recurrence. It was proposed in 2017 by Ashish Vaswani and colleagues at Google Brain. The model reached state-of-the-art quality on machine translation while being much faster to train than recurrent networks.

## Scaled Dot-Product Attention

Attention maps a query and a set of key-value pairs to an output. The weights are computed with a softmax over the scaled dot products of the query with all keys. Softmax turns the raw scores into a probability distribution, so the output is a weighted average of the values.

## Multi-Head Attention

Instead of a single attention function, the Transformer runs several attention heads in parallel. Each head learns to focus on different positions and representation subspaces, and the outputs are concatenated and projected.

## Impact

The Transformer became the foundation of large language models such as BERT and GPT. Retrieval-augmented generation systems also use Transformer encoders to embed documents.
```

`tests/fixtures/vnimanie_ru.md`:

```markdown
# Механизм внимания в нейросетях

## Идея

Механизм внимания позволяет модели фокусироваться на наиболее релевантных частях входной последовательности. Архитектура Transformer, предложенная Ашишем Васвани (Vaswani) в 2017 году, полностью построена на механизме внимания.

## Как считаются веса

Для каждого запроса модель вычисляет скалярные произведения с ключами, масштабирует их и применяет функцию softmax. Softmax превращает оценки в вероятности, и результат — взвешенное среднее значений.

## Применение

Механизм внимания используется в машинном переводе, в языковых моделях и в системах поиска по документам.
```

- [ ] **Step 2: Write the failing test**

`tests/unit/test_parsing_chunking.py`:

```python
from pathlib import Path

from mnogobase.chunking.hybrid import Chunker
from mnogobase.config import ChunkingSettings, ParsingSettings
from mnogobase.parsing.docling_parser import DoclingParser

FIXTURES = Path(__file__).parents[1] / "fixtures"
DOC = "d" * 16


def test_parse_markdown_cache_roundtrip(tmp_path):
    parser = DoclingParser(ParsingSettings(), "cpu", tmp_path)
    parsed = parser.parse(FIXTURES / "attention_en.md", DOC)
    assert parsed.title == "Attention Is All You Need"
    assert parsed.mime == "text/markdown"
    assert parser.cache_path(DOC).exists()
    again = parser.load(DOC, FIXTURES / "attention_en.md")
    assert again.title == parsed.title
    assert again.doc.export_to_markdown() == parsed.doc.export_to_markdown()
    parser.drop_cache(DOC)
    assert not parser.cache_path(DOC).exists()


def test_title_falls_back_to_file_stem(tmp_path):
    src = tmp_path / "notes.md"
    src.write_text("Just a paragraph without headings.\n", encoding="utf-8")
    parsed = DoclingParser(ParsingSettings(), "cpu", tmp_path / "cache").parse(src, DOC)
    assert parsed.title == "notes"


def test_chunker_records(tmp_path):
    parser = DoclingParser(ParsingSettings(), "cpu", tmp_path)
    parsed = parser.parse(FIXTURES / "vnimanie_ru.md", DOC)
    chunks = Chunker(ChunkingSettings(max_tokens=64)).chunk(parsed, DOC, "/x/vnimanie_ru.md")
    assert len(chunks) >= 2
    assert [c.idx for c in chunks] == list(range(len(chunks)))
    assert chunks[0].chunk_id == f"{DOC}:00000"
    assert all(c.text and c.n_tokens > 0 for c in chunks)
    assert max(c.n_tokens for c in chunks) <= 96
    assert any("Как считаются веса" in c.headings for c in chunks)
    first = chunks[0]
    assert first.headings and first.context_text.startswith(first.headings[0])
    assert all(c.path == "/x/vnimanie_ru.md" and c.doc_id == DOC for c in chunks)
    assert all(c.page_start is None for c in chunks)  # markdown has no pages
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run pytest tests/unit/test_parsing_chunking.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mnogobase.parsing'`.

- [ ] **Step 4: Implement the parser and chunker**

`src/mnogobase/parsing/docling_parser.py`:

```python
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.types.doc import DocItemLabel, DoclingDocument, PictureItem

from mnogobase.config import ParsingSettings
from mnogobase.log import get_logger


@dataclass
class ParsedDocument:
    doc: DoclingDocument
    title: str
    mime: str
    n_pages: int | None


class DoclingParser:
    def __init__(self, settings: ParsingSettings, device: str, cache_dir: Path):
        self._s = settings
        self._device = device
        self._cache = cache_dir
        self._converter: DocumentConverter | None = None
        self._log = get_logger(__name__)

    def _get_converter(self) -> DocumentConverter:
        if self._converter is None:
            pdf_options = PdfPipelineOptions(
                do_ocr=self._s.ocr,
                generate_picture_images=True,  # kept for future multimodal embedding
                images_scale=2.0,
                accelerator_options=AcceleratorOptions(device=AcceleratorDevice(self._device)),
            )
            self._converter = DocumentConverter(
                format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_options)}
            )
        return self._converter

    def cache_path(self, doc_id: str) -> Path:
        return self._cache / f"{doc_id}.json"

    def parse(self, path: Path, doc_id: str) -> ParsedDocument:
        result = self._get_converter().convert(path, raises_on_error=True)
        doc = result.document
        self._cache.mkdir(parents=True, exist_ok=True)
        self.cache_path(doc_id).write_text(doc.model_dump_json(), encoding="utf-8")
        n_images = _save_images(doc, self._cache / doc_id / "images")
        parsed = _wrap(doc, path)
        self._log.info("parsed", doc_id=doc_id, title=parsed.title, n_pages=parsed.n_pages,
                       n_images=n_images)
        return parsed

    def load(self, doc_id: str, path: Path) -> ParsedDocument:
        doc = DoclingDocument.model_validate_json(self.cache_path(doc_id).read_text(encoding="utf-8"))
        return _wrap(doc, path)

    def drop_cache(self, doc_id: str) -> None:
        self.cache_path(doc_id).unlink(missing_ok=True)
        shutil.rmtree(self._cache / doc_id, ignore_errors=True)


def _wrap(doc: DoclingDocument, path: Path) -> ParsedDocument:
    mime = doc.origin.mimetype if doc.origin else "application/octet-stream"
    return ParsedDocument(doc=doc, title=_title(doc, path), mime=mime, n_pages=len(doc.pages) or None)


def _title(doc: DoclingDocument, path: Path) -> str:
    for item, _level in doc.iterate_items():
        if getattr(item, "label", None) == DocItemLabel.TITLE and item.text.strip():
            return item.text.strip()
    return path.stem


def _save_images(doc: DoclingDocument, out_dir: Path) -> int:
    manifest = []
    for item, _level in doc.iterate_items():
        if not isinstance(item, PictureItem):
            continue
        image = item.get_image(doc)
        if image is None:
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        name = f"img_{len(manifest):04d}.png"
        image.save(out_dir / name)
        manifest.append({
            "file": name,
            "caption": item.caption_text(doc),
            "page": item.prov[0].page_no if item.prov else None,
        })
    if manifest:
        (out_dir / "images.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return len(manifest)
```

`src/mnogobase/chunking/hybrid.py`:

```python
from __future__ import annotations

from docling_core.transforms.chunker.hybrid_chunker import HybridChunker
from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer

from mnogobase.config import ChunkingSettings
from mnogobase.ids import chunk_id
from mnogobase.models import ChunkRecord
from mnogobase.parsing.docling_parser import ParsedDocument


class Chunker:
    """Structure- and token-aware chunking with the embedder's own tokenizer."""

    def __init__(self, settings: ChunkingSettings):
        self._s = settings
        self._chunker: HybridChunker | None = None
        self._tokenizer: HuggingFaceTokenizer | None = None

    def _get(self) -> tuple[HybridChunker, HuggingFaceTokenizer]:
        if self._chunker is None:
            from transformers import AutoTokenizer

            self._tokenizer = HuggingFaceTokenizer(
                tokenizer=AutoTokenizer.from_pretrained(self._s.tokenizer),
                max_tokens=self._s.max_tokens,
            )
            self._chunker = HybridChunker(tokenizer=self._tokenizer, merge_peers=True)
        return self._chunker, self._tokenizer

    def chunk(self, parsed: ParsedDocument, doc_id: str, path: str) -> list[ChunkRecord]:
        chunker, tokenizer = self._get()
        records: list[ChunkRecord] = []
        for raw in chunker.chunk(parsed.doc):
            text = raw.text.strip()
            if not text:
                continue
            pages = sorted({p.page_no for item in raw.meta.doc_items for p in item.prov})
            context = chunker.contextualize(raw)
            idx = len(records)
            records.append(
                ChunkRecord(
                    chunk_id=chunk_id(doc_id, idx),
                    doc_id=doc_id,
                    idx=idx,
                    text=text,
                    context_text=context,
                    headings=list(raw.meta.headings or []),
                    page_start=pages[0] if pages else None,
                    page_end=pages[-1] if pages else None,
                    n_tokens=tokenizer.count_tokens(context),
                    path=path,
                )
            )
        return records
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/unit/test_parsing_chunking.py -v`
Expected: 3 passed (first run downloads the `google/embeddinggemma-2` tokenizer).

- [ ] **Step 6: Commit**

```bash
git add src/mnogobase/parsing src/mnogobase/chunking tests/fixtures tests/unit/test_parsing_chunking.py
git commit -m "feat: docling parser with cache and hybrid chunker"
```

---

### Task 8: Qdrant vector store

**Files:**
- Create: `src/mnogobase/stores/__init__.py` (empty), `src/mnogobase/stores/qdrant_store.py`
- Test: `tests/unit/test_qdrant_store.py`

**Interfaces:**
- Consumes: `ids.point_id` (Task 2); `models.ChunkRecord`, `EntityRecord`, `SearchHit` (Task 2); `config.QdrantSettings` (Task 1); `tests.fakes.FakeEmbedder`, `FakeSparse` (Task 6).
- Produces: `DimensionMismatchError(RuntimeError)`; `QdrantStore(client: QdrantClient, prefix: str, dim: int)` with attributes `client`, `dim`, `chunks`, `entities`, `wiki` (collection names) and methods:
  `from_settings(settings: QdrantSettings, dim: int) -> QdrantStore` (classmethod), `ensure_collections() -> None`, `collection_dim(name: str) -> int | None`, `drop_collections() -> None`,
  `upsert_chunks(chunks: list[ChunkRecord], dense: list[list[float]], sparse: list[SparseVector]) -> None`, `set_chunk_entities(chunk_id: str, entity_ids: list[str]) -> None`, `delete_doc(doc_id: str) -> None`,
  `search_chunks(dense: list[float], sparse: SparseVector, k: int) -> list[SearchHit]` (key = `chunk_id`), `search_chunks_for_entity(dense: list[float], entity_id: str, k: int) -> list[SearchHit]`,
  `upsert_entities(entities: list[EntityRecord], dense: list[list[float]]) -> None`, `search_entities(dense: list[float], k: int, score_threshold: float | None = None) -> list[SearchHit]` (key = `entity_id`), `delete_entities(entity_ids: list[str]) -> None`,
  `upsert_wiki_sections(page_id: str, entity_id: str, path: str, sections: list[tuple[str, str]], dense: list[list[float]], sparse: list[SparseVector]) -> None`, `delete_wiki_page(page_id: str) -> None`, `search_wiki(dense, sparse, k) -> list[SearchHit]` (key = `f"{page_id}#{i}"`).
  Chunk payload keys: `chunk_id, doc_id, text, headings, page, path, modality, entity_ids`. Wiki payload keys: `key, page_id, entity_id, path, section, text`.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_qdrant_store.py`:

```python
import pytest
from qdrant_client import QdrantClient

from mnogobase.models import ChunkRecord, EmbedInput, EntityRecord
from mnogobase.stores.qdrant_store import DimensionMismatchError, QdrantStore
from tests.fakes import FakeEmbedder, FakeSparse

EMB = FakeEmbedder()
SP = FakeSparse()


def make_store(client: QdrantClient | None = None, dim: int = 64) -> QdrantStore:
    store = QdrantStore(client or QdrantClient(":memory:"), "t_", dim)
    store.ensure_collections()
    return store


def chunks(doc_id: str, texts: list[str]) -> list[ChunkRecord]:
    return [
        ChunkRecord(chunk_id=f"{doc_id}:{i:05d}", doc_id=doc_id, idx=i, text=t, context_text=t,
                    path=f"/{doc_id}.md")
        for i, t in enumerate(texts)
    ]


def index(store: QdrantStore, items: list[ChunkRecord]) -> None:
    texts = [c.context_text for c in items]
    store.upsert_chunks(items, EMB.embed_documents([EmbedInput(text=t) for t in texts]),
                        SP.encode_documents(texts))


def test_ensure_is_idempotent_and_checks_dimension():
    client = QdrantClient(":memory:")
    make_store(client)
    make_store(client)
    with pytest.raises(DimensionMismatchError, match="reindex"):
        QdrantStore(client, "t_", 32).ensure_collections()


def test_hybrid_search_ranks_relevant_chunk_first():
    store = make_store()
    index(store, chunks("aaaa", ["the transformer uses attention", "cats are cute animals",
                                 "softmax normalizes scores"]))
    q = "attention transformer"
    hits = store.search_chunks(EMB.embed_query(q), SP.encode_query(q), k=2)
    assert len(hits) == 2
    assert hits[0].key == "aaaa:00000"
    assert hits[0].payload["path"] == "/aaaa.md"
    assert hits[0].payload["entity_ids"] == []


def test_delete_doc_only_removes_that_doc():
    store = make_store()
    index(store, chunks("aaaa", ["one", "two"]))
    index(store, chunks("bbbb", ["three"]))
    store.delete_doc("aaaa")
    assert store.client.count(store.chunks).count == 1


def test_chunk_entity_filter():
    store = make_store()
    index(store, chunks("aaaa", ["transformer attention", "cats and dogs"]))
    store.set_chunk_entities("aaaa:00001", ["e2", "e1", "e1"])
    hits = store.search_chunks_for_entity(EMB.embed_query("cats"), "e1", k=5)
    assert [h.key for h in hits] == ["aaaa:00001"]
    assert hits[0].payload["entity_ids"] == ["e1", "e2"]


def test_entity_search_threshold_and_delete():
    store = make_store()
    recs = [
        EntityRecord(entity_id="e1", name="Transformer", type="Method", description="attention architecture"),
        EntityRecord(entity_id="e2", name="Cat", type="Concept", description="small animal"),
    ]
    store.upsert_entities(recs, EMB.embed_documents(
        [EmbedInput(text=f"{r.name}: {r.description}") for r in recs]))
    hits = store.search_entities(EMB.embed_query("Transformer: attention architecture"), k=2,
                                 score_threshold=0.9)
    assert [h.key for h in hits] == ["e1"]
    assert hits[0].score > 0.99
    store.delete_entities(["e1"])
    assert store.client.count(store.entities).count == 1


def test_wiki_sections_replace_previous_version():
    store = make_store()
    sections = [("Summary", "transformer summary"), ("Details", "attention details")]
    texts = [t for _, t in sections]
    store.upsert_wiki_sections("p1", "e1", "entities/transformer.md", sections,
                               EMB.embed_documents([EmbedInput(text=t) for t in texts]),
                               SP.encode_documents(texts))
    assert store.client.count(store.wiki).count == 2
    store.upsert_wiki_sections("p1", "e1", "entities/transformer.md", sections[:1],
                               EMB.embed_documents([EmbedInput(text=texts[0])]),
                               SP.encode_documents(texts[:1]))
    assert store.client.count(store.wiki).count == 1
    hits = store.search_wiki(EMB.embed_query("transformer"), SP.encode_query("transformer"), k=3)
    assert hits[0].key == "p1#0"
    assert hits[0].payload["path"] == "entities/transformer.md"
    store.delete_wiki_page("p1")
    assert store.client.count(store.wiki).count == 0
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/unit/test_qdrant_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mnogobase.stores'`.

- [ ] **Step 3: Implement `qdrant_store.py`**

```python
from __future__ import annotations

from qdrant_client import QdrantClient
from qdrant_client import models as qm

from mnogobase.config import QdrantSettings
from mnogobase.ids import point_id
from mnogobase.models import ChunkRecord, EntityRecord, SearchHit

DENSE = "dense"
SPARSE = "bm25"
_KEYWORD = qm.PayloadSchemaType.KEYWORD


class DimensionMismatchError(RuntimeError):
    pass


def _match(key: str, value: str) -> qm.Filter:
    return qm.Filter(must=[qm.FieldCondition(key=key, match=qm.MatchValue(value=value))])


class QdrantStore:
    def __init__(self, client: QdrantClient, prefix: str, dim: int):
        self.client = client
        self.dim = dim
        self.chunks = f"{prefix}chunks"
        self.entities = f"{prefix}entities"
        self.wiki = f"{prefix}wiki_pages"

    @classmethod
    def from_settings(cls, settings: QdrantSettings, dim: int) -> QdrantStore:
        return cls(QdrantClient(url=settings.url, timeout=60), settings.prefix, dim)

    # ---- collections ----
    def ensure_collections(self) -> None:
        self._ensure(self.chunks, sparse=True, indexes=("chunk_id", "doc_id", "entity_ids", "modality"))
        self._ensure(self.entities, sparse=False, indexes=("entity_id", "type"))
        self._ensure(self.wiki, sparse=True, indexes=("page_id", "entity_id"))

    def _ensure(self, name: str, *, sparse: bool, indexes: tuple[str, ...]) -> None:
        if self.client.collection_exists(name):
            existing = self.collection_dim(name)
            if existing != self.dim:
                raise DimensionMismatchError(
                    f"collection {name} has dim {existing}, the embedder produces {self.dim}; "
                    "run `mnogobase reindex`"
                )
            return
        self.client.create_collection(
            name,
            vectors_config={DENSE: qm.VectorParams(size=self.dim, distance=qm.Distance.COSINE)},
            sparse_vectors_config=(
                {SPARSE: qm.SparseVectorParams(modifier=qm.Modifier.IDF)} if sparse else None
            ),
        )
        for field in indexes:
            self.client.create_payload_index(name, field, field_schema=_KEYWORD)

    def collection_dim(self, name: str) -> int | None:
        if not self.client.collection_exists(name):
            return None
        vectors = self.client.get_collection(name).config.params.vectors
        return vectors[DENSE].size

    def drop_collections(self) -> None:
        for name in (self.chunks, self.entities, self.wiki):
            if self.client.collection_exists(name):
                self.client.delete_collection(name)

    def _upsert(self, name: str, points: list[qm.PointStruct], batch: int = 256) -> None:
        for start in range(0, len(points), batch):
            self.client.upsert(name, points[start : start + batch], wait=True)

    def _hybrid(self, name, dense, sparse, k: int, key: str) -> list[SearchHit]:
        response = self.client.query_points(
            name,
            prefetch=[
                qm.Prefetch(query=dense, using=DENSE, limit=k * 4),
                qm.Prefetch(query=sparse, using=SPARSE, limit=k * 4),
            ],
            query=qm.FusionQuery(fusion=qm.Fusion.RRF),
            limit=k,
            with_payload=True,
        )
        return [SearchHit(key=p.payload[key], score=p.score, payload=p.payload) for p in response.points]

    # ---- chunks ----
    def upsert_chunks(self, chunks: list[ChunkRecord], dense, sparse) -> None:
        points = [
            qm.PointStruct(
                id=point_id(c.chunk_id),
                vector={DENSE: d, SPARSE: s},
                payload={
                    "chunk_id": c.chunk_id, "doc_id": c.doc_id, "text": c.text,
                    "headings": c.headings, "page": c.page_start, "path": c.path,
                    "modality": c.modality, "entity_ids": [],
                },
            )
            for c, d, s in zip(chunks, dense, sparse, strict=True)
        ]
        self._upsert(self.chunks, points)

    def set_chunk_entities(self, chunk_id: str, entity_ids: list[str]) -> None:
        self.client.set_payload(
            self.chunks, {"entity_ids": sorted(set(entity_ids))}, points=[point_id(chunk_id)]
        )

    def delete_doc(self, doc_id: str) -> None:
        self.client.delete(self.chunks, points_selector=qm.FilterSelector(filter=_match("doc_id", doc_id)))

    def search_chunks(self, dense, sparse, k: int) -> list[SearchHit]:
        return self._hybrid(self.chunks, dense, sparse, k, key="chunk_id")

    def search_chunks_for_entity(self, dense, entity_id: str, k: int) -> list[SearchHit]:
        response = self.client.query_points(
            self.chunks, query=dense, using=DENSE, query_filter=_match("entity_ids", entity_id),
            limit=k, with_payload=True,
        )
        return [SearchHit(key=p.payload["chunk_id"], score=p.score, payload=p.payload)
                for p in response.points]

    # ---- entities ----
    def upsert_entities(self, entities: list[EntityRecord], dense) -> None:
        points = [
            qm.PointStruct(
                id=point_id(e.entity_id),
                vector={DENSE: d},
                payload={"entity_id": e.entity_id, "name": e.name, "type": e.type},
            )
            for e, d in zip(entities, dense, strict=True)
        ]
        self._upsert(self.entities, points)

    def search_entities(self, dense, k: int, score_threshold: float | None = None) -> list[SearchHit]:
        response = self.client.query_points(
            self.entities, query=dense, using=DENSE, limit=k, with_payload=True,
            score_threshold=score_threshold,
        )
        return [SearchHit(key=p.payload["entity_id"], score=p.score, payload=p.payload)
                for p in response.points]

    def delete_entities(self, entity_ids: list[str]) -> None:
        if entity_ids:
            self.client.delete(
                self.entities,
                points_selector=qm.PointIdsList(points=[point_id(e) for e in entity_ids]),
            )

    # ---- wiki ----
    def upsert_wiki_sections(self, page_id: str, entity_id: str, path: str,
                             sections: list[tuple[str, str]], dense, sparse) -> None:
        self.delete_wiki_page(page_id)
        points = []
        for i, ((title, text), d, s) in enumerate(zip(sections, dense, sparse, strict=True)):
            key = f"{page_id}#{i}"
            points.append(qm.PointStruct(
                id=point_id(key),
                vector={DENSE: d, SPARSE: s},
                payload={"key": key, "page_id": page_id, "entity_id": entity_id, "path": path,
                         "section": title, "text": text},
            ))
        self._upsert(self.wiki, points)

    def delete_wiki_page(self, page_id: str) -> None:
        self.client.delete(self.wiki, points_selector=qm.FilterSelector(filter=_match("page_id", page_id)))

    def search_wiki(self, dense, sparse, k: int) -> list[SearchHit]:
        return self._hybrid(self.wiki, dense, sparse, k, key="key")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/unit/test_qdrant_store.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mnogobase/stores tests/unit/test_qdrant_store.py
git commit -m "feat: qdrant store with hybrid dense+bm25 search and wiki sections"
```

---

### Task 9: Neo4j graph store

**Files:**
- Create: `src/mnogobase/stores/graph_store.py`
- Create: `tests/conftest.py`
- Test: `tests/integration/test_graph_store.py`

**Interfaces:**
- Consumes: `models.*` (Task 2), `config.Neo4jSettings` (Task 1).
- Produces:
  - `DeleteResult(affected: list[str], removed_entity_ids: list[str], removed_names: list[str], removed_pages: list[tuple[str, str]])` (dataclass; `removed_pages` items are `(page_id, relative_path)`).
  - `GraphStore(driver: neo4j.Driver, database: str | None = None)` with:
    `from_settings(settings: Neo4jSettings) -> GraphStore` (classmethod), `close()`, `verify()`, `ensure_schema()`, `wipe()`, `counts() -> dict[str, int]` (keys `Document, Chunk, Entity, WikiPage, RELATED`),
    `upsert_document(doc: DocumentRecord)`, `upsert_chunks(chunks: list[ChunkRecord])`, `delete_document(doc_id: str) -> DeleteResult`, `chunks_by_ids(chunk_ids: list[str]) -> list[ChunkView]`, `chunk_entity_ids(doc_id: str) -> dict[str, list[str]]`,
    `get_entity(entity_id: str) -> EntityRecord | None`, `upsert_entity(entity: EntityRecord)`, `entities(min_mentions: int = 0) -> list[EntityRecord]`, `add_mentions(chunk_id: str, entity_ids: list[str])`,
    `merge_relation(src_id: str, dst_id: str, predicate: str, description: str, strength: int, chunk_id: str)`, `entity_context(entity_id: str, max_relations: int) -> EntityContext`,
    `fulltext_entities(query: str, k: int) -> list[tuple[str, float]]`, `neighborhood(seed_ids: list[str], hops: int, limit: int) -> list[RelationView]`,
    `upsert_wiki_page(page: WikiPageRecord, entity_id: str, links_to: list[str], cites: list[str])`, `wiki_page(page_id: str) -> WikiPageRecord | None`, `page_by_slug(slug: str) -> WikiPageRecord | None`, `wiki_pages() -> list[PageIndexRow]`.
  - `tests/conftest.py` fixtures `neo4j_container` (session) and `graph` (fresh, wiped `GraphStore`).

- [ ] **Step 1: Create `tests/conftest.py`**

```python
import pytest

NEO4J_IMAGE = "neo4j:5.26-community"


@pytest.fixture(scope="session")
def neo4j_container():
    from testcontainers.neo4j import Neo4jContainer

    with Neo4jContainer(NEO4J_IMAGE, password="testpassword") as container:
        yield container


@pytest.fixture
def graph(neo4j_container):
    from mnogobase.stores.graph_store import GraphStore

    store = GraphStore(neo4j_container.get_driver())
    store.wipe()
    store.ensure_schema()
    yield store
    store.close()
```

- [ ] **Step 2: Write the failing test**

`tests/integration/test_graph_store.py`:

```python
import pytest

from mnogobase.models import ChunkRecord, DocumentRecord, EntityRecord, WikiPageRecord

pytestmark = pytest.mark.integration


def make_doc(graph, doc_id: str = "d" * 16, n: int = 3) -> list[ChunkRecord]:
    graph.upsert_document(DocumentRecord(doc_id=doc_id, path=f"/{doc_id}.md", title="T",
                                         mime="text/markdown"))
    chunks = [ChunkRecord(chunk_id=f"{doc_id}:{i:05d}", doc_id=doc_id, idx=i, text=f"text {i}",
                          context_text=f"text {i}", page_start=i + 1) for i in range(n)]
    graph.upsert_chunks(chunks)
    return chunks


def make_entity(graph, eid: str, name: str, etype: str = "Concept", aliases=()) -> EntityRecord:
    rec = EntityRecord(entity_id=eid, name=name, type=etype, aliases=list(aliases),
                       description=f"{name} desc", descriptions=[f"{name} desc"])
    graph.upsert_entity(rec)
    return rec


def test_document_chunks_next_and_idempotency(graph):
    chunks = make_doc(graph)
    views = sorted(graph.chunks_by_ids([c.chunk_id for c in chunks]), key=lambda v: v.chunk_id)
    assert [v.chunk_id for v in views] == [c.chunk_id for c in chunks]
    assert views[0].path == f"/{'d' * 16}.md" and views[0].page == 1
    n_next = graph._run("MATCH (:Chunk)-[r:NEXT]->(:Chunk) RETURN count(r) AS n")[0]["n"]
    assert n_next == 2
    make_doc(graph)
    assert graph.counts()["Chunk"] == 3


def test_mentions_and_relations_are_idempotent(graph):
    chunks = make_doc(graph)
    make_entity(graph, "e1", "Transformer")
    make_entity(graph, "e2", "Softmax")
    graph.add_mentions(chunks[0].chunk_id, ["e1", "e2"])
    graph.add_mentions(chunks[1].chunk_id, ["e1"])
    graph.add_mentions(chunks[1].chunk_id, ["e1"])
    assert graph.get_entity("e1").mention_count == 2
    graph.merge_relation("e1", "e2", "uses", "T uses softmax", 5, chunks[0].chunk_id)
    graph.merge_relation("e1", "e2", "uses", "T uses softmax", 5, chunks[0].chunk_id)
    graph.merge_relation("e1", "e2", "uses", "Transformer uses softmax in attention", 3,
                         chunks[1].chunk_id)
    rels = graph.entity_context("e1", max_relations=10).relations
    assert len(rels) == 1
    assert rels[0].weight == 8
    assert sorted(rels[0].evidence) == [chunks[0].chunk_id, chunks[1].chunk_id]
    assert rels[0].description == "Transformer uses softmax in attention"
    assert graph.chunk_entity_ids("d" * 16)[chunks[0].chunk_id] in (["e1", "e2"], ["e2", "e1"])


def test_delete_document_cascade(graph):
    a = make_doc(graph, "a" * 16, 2)
    b = make_doc(graph, "b" * 16, 1)
    make_entity(graph, "e1", "Shared")
    make_entity(graph, "e2", "Only A")
    make_entity(graph, "e3", "Only B")
    graph.add_mentions(a[0].chunk_id, ["e1", "e2"])
    graph.add_mentions(b[0].chunk_id, ["e1", "e3"])
    graph.merge_relation("e1", "e2", "uses", "", 4, a[0].chunk_id)
    graph.merge_relation("e1", "e3", "uses", "", 4, b[0].chunk_id)
    graph.merge_relation("e1", "e3", "cites", "", 2, a[1].chunk_id)
    graph.upsert_wiki_page(WikiPageRecord(page_id="e2", slug="only-a", title="Only A",
                                          path="entities/only-a.md"), "e2", [], [a[0].chunk_id])

    result = graph.delete_document("a" * 16)

    assert sorted(result.affected) == ["e1", "e2"]
    assert result.removed_entity_ids == ["e2"]
    assert result.removed_names == ["Only A"]
    assert result.removed_pages == [("e2", "entities/only-a.md")]
    assert graph.get_entity("e2") is None
    assert graph.get_entity("e1").mention_count == 1
    rels = graph.entity_context("e1", max_relations=10).relations
    assert [(r.predicate, r.dst_id) for r in rels] == [("uses", "e3")]
    assert graph.counts()["Document"] == 1
    assert graph.wiki_page("e2") is None


def test_fulltext_special_characters(graph):
    make_entity(graph, "e1", "C++", aliases=["cpp"])
    make_entity(graph, "e2", "Transformer")
    for query in ['C++ AND (foo)', 'what is "transformer"?', "a/b: c~ OR NOT", "***", ""]:
        graph.fulltext_entities(query, k=5)  # must not raise
    assert [eid for eid, _ in graph.fulltext_entities("tell me about the transformer", 5)] == ["e2"]
    assert "e1" in [eid for eid, _ in graph.fulltext_entities("cpp", 5)]


def test_neighborhood_hops_and_ranking(graph):
    chunks = make_doc(graph)
    for i in range(1, 5):
        make_entity(graph, f"e{i}", f"Entity {i}")
    graph.merge_relation("e1", "e2", "r", "", 9, chunks[0].chunk_id)
    graph.merge_relation("e2", "e3", "r", "", 2, chunks[0].chunk_id)
    graph.merge_relation("e3", "e4", "r", "", 5, chunks[0].chunk_id)
    two = graph.neighborhood(["e1"], hops=2, limit=10)
    assert {(r.src_id, r.dst_id) for r in two} == {("e1", "e2"), ("e2", "e3")}
    assert two[0].weight == 9
    one = graph.neighborhood(["e1"], hops=1, limit=10)
    assert [(r.src_id, r.dst_id) for r in one] == [("e1", "e2")]


def test_wiki_page_links_are_replaced(graph):
    chunks = make_doc(graph)
    for eid, name in (("e1", "One"), ("e2", "Two"), ("e3", "Three")):
        make_entity(graph, eid, name, etype="Method")
        graph.upsert_wiki_page(WikiPageRecord(page_id=eid, slug=name.lower(), title=name,
                                              path=f"entities/{name.lower()}.md"), eid, [], [])
    page = WikiPageRecord(page_id="e1", slug="one", title="One", path="entities/one.md", version=2)
    graph.upsert_wiki_page(page, "e1", ["e2"], [chunks[0].chunk_id])
    graph.upsert_wiki_page(page, "e1", ["e3"], [chunks[1].chunk_id])
    links = graph._run("MATCH (:WikiPage {page_id:'e1'})-[:LINKS_TO]->(p) RETURN p.page_id AS id")
    cites = graph._run("MATCH (:WikiPage {page_id:'e1'})-[:CITES]->(c) RETURN c.chunk_id AS id")
    assert [r["id"] for r in links] == ["e3"]
    assert [r["id"] for r in cites] == [chunks[1].chunk_id]
    assert graph.wiki_page("e1").version == 2
    assert graph.page_by_slug("two").page_id == "e2"
    rows = graph.wiki_pages()
    assert {r.page_id for r in rows} == {"e1", "e2", "e3"}
    assert all(r.entity_type == "Method" for r in rows)
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `open -a Docker` (if the daemon is not running), then `uv run pytest -m integration tests/integration/test_graph_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mnogobase.stores.graph_store'`.

- [ ] **Step 4: Implement `graph_store.py`**

```python
from __future__ import annotations

import re
from dataclasses import dataclass

from neo4j import Driver, GraphDatabase

from mnogobase.config import Neo4jSettings
from mnogobase.models import (
    ChunkRecord,
    ChunkView,
    DocumentRecord,
    EntityContext,
    EntityRecord,
    PageIndexRow,
    RelationView,
    WikiPageRecord,
)

_SCHEMA = [
    "CREATE CONSTRAINT document_id IF NOT EXISTS FOR (d:Document) REQUIRE d.doc_id IS UNIQUE",
    "CREATE CONSTRAINT chunk_id IF NOT EXISTS FOR (c:Chunk) REQUIRE c.chunk_id IS UNIQUE",
    "CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (e:Entity) REQUIRE e.entity_id IS UNIQUE",
    "CREATE CONSTRAINT page_id IF NOT EXISTS FOR (p:WikiPage) REQUIRE p.page_id IS UNIQUE",
    "CREATE INDEX page_slug IF NOT EXISTS FOR (p:WikiPage) ON (p.slug)",
    "CREATE INDEX chunk_doc IF NOT EXISTS FOR (c:Chunk) ON (c.doc_id)",
    "CREATE FULLTEXT INDEX entity_names IF NOT EXISTS FOR (e:Entity) ON EACH [e.name, e.aliases_text]",
]
_WORD = re.compile(r"\w+", re.UNICODE)
_PAGE_FIELDS = "p {.page_id, .slug, .title, .path, .content_hash, .version} AS page"
_REL_MAP = (
    "{src_id: startNode(r).entity_id, src_name: startNode(r).name, predicate: r.predicate, "
    "dst_id: endNode(r).entity_id, dst_name: endNode(r).name, description: r.description, "
    "weight: r.weight, evidence: r.evidence}"
)


@dataclass
class DeleteResult:
    affected: list[str]
    removed_entity_ids: list[str]
    removed_names: list[str]
    removed_pages: list[tuple[str, str]]


class GraphStore:
    def __init__(self, driver: Driver, database: str | None = None):
        self._driver = driver
        self._db = database

    @classmethod
    def from_settings(cls, settings: Neo4jSettings) -> GraphStore:
        return cls(GraphDatabase.driver(settings.uri, auth=(settings.user, settings.password())))

    def close(self) -> None:
        self._driver.close()

    def verify(self) -> None:
        self._driver.verify_connectivity()

    def _run(self, query: str, **params) -> list[dict]:
        records, _, _ = self._driver.execute_query(query, parameters_=params, database_=self._db)
        return [r.data() for r in records]

    # ---- schema / maintenance ----
    def ensure_schema(self) -> None:
        for statement in _SCHEMA:
            self._run(statement)
        self._run("CALL db.awaitIndexes(60)")

    def wipe(self) -> None:
        self._run("MATCH (n) DETACH DELETE n")

    def counts(self) -> dict[str, int]:
        out = {label: self._run(f"MATCH (n:{label}) RETURN count(n) AS n")[0]["n"]
               for label in ("Document", "Chunk", "Entity", "WikiPage")}
        out["RELATED"] = self._run("MATCH ()-[r:RELATED]->() RETURN count(r) AS n")[0]["n"]
        return out

    # ---- documents / chunks ----
    def upsert_document(self, doc: DocumentRecord) -> None:
        self._run(
            "MERGE (d:Document {doc_id: $doc_id}) "
            "SET d.path = $path, d.title = $title, d.mime = $mime, d.n_pages = $n_pages, "
            "d.ingested_at = datetime()",
            **doc.model_dump(),
        )

    def upsert_chunks(self, chunks: list[ChunkRecord]) -> None:
        rows = [c.model_dump(exclude={"context_text", "path"}) for c in chunks]
        self._run(
            "UNWIND $rows AS row "
            "MATCH (d:Document {doc_id: row.doc_id}) "
            "MERGE (c:Chunk {chunk_id: row.chunk_id}) "
            "SET c += row {.doc_id, .idx, .text, .headings, .page_start, .page_end, .n_tokens, .modality} "
            "MERGE (d)-[:HAS_CHUNK]->(c)",
            rows=rows,
        )
        pairs = [[a.chunk_id, b.chunk_id] for a, b in zip(chunks, chunks[1:])]
        if pairs:
            self._run(
                "UNWIND $pairs AS p "
                "MATCH (a:Chunk {chunk_id: p[0]}), (b:Chunk {chunk_id: p[1]}) "
                "MERGE (a)-[:NEXT]->(b)",
                pairs=pairs,
            )

    def delete_document(self, doc_id: str) -> DeleteResult:
        found = self._run(
            "MATCH (d:Document {doc_id: $doc_id}) "
            "OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:Chunk) "
            "OPTIONAL MATCH (c)-[:MENTIONS]->(e:Entity) "
            "RETURN collect(DISTINCT c.chunk_id) AS cids, collect(DISTINCT e.entity_id) AS eids",
            doc_id=doc_id,
        )
        cids = found[0]["cids"] if found else []
        eids = found[0]["eids"] if found else []
        if cids:
            # drop evidence coming from the deleted chunks; edges left without evidence disappear
            self._run(
                "MATCH ()-[r:RELATED]->() WHERE any(x IN r.evidence WHERE x IN $cids) "
                "WITH r, [i IN range(0, size(r.evidence) - 1) WHERE NOT r.evidence[i] IN $cids] AS keep "
                "SET r.evidence = [i IN keep | r.evidence[i]], "
                "    r.strengths = [i IN keep | r.strengths[i]] "
                "SET r.weight = reduce(s = 0.0, x IN r.strengths | s + x) "
                "WITH r WHERE size(r.evidence) = 0 DELETE r",
                cids=cids,
            )
        self._run(
            "MATCH (d:Document {doc_id: $doc_id}) "
            "OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:Chunk) "
            "WITH d, collect(c) AS cs "
            "FOREACH (x IN cs | DETACH DELETE x) "
            "DETACH DELETE d",
            doc_id=doc_id,
        )
        removed = self._run(
            "UNWIND $eids AS eid "
            "MATCH (e:Entity {entity_id: eid}) "
            "SET e.mention_count = COUNT { (e)<-[:MENTIONS]-(:Chunk) } "
            "WITH e WHERE e.mention_count = 0 "
            "OPTIONAL MATCH (p:WikiPage)-[:ABOUT]->(e) "
            "WITH e, e.entity_id AS eid, e.name AS name, collect(p) AS pages "
            "WITH e, eid, name, pages, [x IN pages | [x.page_id, x.path]] AS page_refs "
            "FOREACH (x IN pages | DETACH DELETE x) "
            "DETACH DELETE e "
            "RETURN eid, name, page_refs",
            eids=eids,
        )
        return DeleteResult(
            affected=eids,
            removed_entity_ids=[r["eid"] for r in removed],
            removed_names=[r["name"] for r in removed],
            removed_pages=[(p[0], p[1]) for r in removed for p in r["page_refs"]],
        )

    def chunks_by_ids(self, chunk_ids: list[str]) -> list[ChunkView]:
        rows = self._run(
            "UNWIND $ids AS cid "
            "MATCH (d:Document)-[:HAS_CHUNK]->(c:Chunk {chunk_id: cid}) "
            "RETURN c.chunk_id AS chunk_id, c.doc_id AS doc_id, c.text AS text, d.path AS path, "
            "c.page_start AS page, coalesce(c.headings, []) AS headings",
            ids=chunk_ids,
        )
        return [ChunkView(**r) for r in rows]

    def chunk_entity_ids(self, doc_id: str) -> dict[str, list[str]]:
        rows = self._run(
            "MATCH (c:Chunk {doc_id: $doc_id}) "
            "OPTIONAL MATCH (c)-[:MENTIONS]->(e:Entity) "
            "RETURN c.chunk_id AS cid, collect(e.entity_id) AS eids",
            doc_id=doc_id,
        )
        return {r["cid"]: r["eids"] for r in rows}

    # ---- entities / relations ----
    def get_entity(self, entity_id: str) -> EntityRecord | None:
        rows = self._run("MATCH (e:Entity {entity_id: $id}) RETURN e {.*} AS e", id=entity_id)
        return EntityRecord(**rows[0]["e"]) if rows else None

    def upsert_entity(self, entity: EntityRecord) -> None:
        self._run(
            "MERGE (e:Entity {entity_id: $entity_id}) "
            "SET e.name = $name, e.type = $type, e.aliases = $aliases, e.aliases_text = $aliases_text, "
            "e.description = $description, e.descriptions = $descriptions, "
            "e.mention_count = coalesce(e.mention_count, 0)",
            entity_id=entity.entity_id, name=entity.name, type=entity.type,
            aliases=entity.aliases, aliases_text=" | ".join(entity.aliases),
            description=entity.description, descriptions=entity.descriptions,
        )

    def entities(self, min_mentions: int = 0) -> list[EntityRecord]:
        rows = self._run(
            "MATCH (e:Entity) WHERE e.mention_count >= $m RETURN e {.*} AS e ORDER BY e.name",
            m=min_mentions,
        )
        return [EntityRecord(**r["e"]) for r in rows]

    def add_mentions(self, chunk_id: str, entity_ids: list[str]) -> None:
        if not entity_ids:
            return
        self._run(
            "MATCH (c:Chunk {chunk_id: $chunk_id}) "
            "UNWIND $eids AS eid "
            "MATCH (e:Entity {entity_id: eid}) "
            "MERGE (c)-[:MENTIONS]->(e) "
            "WITH DISTINCT e "
            "SET e.mention_count = COUNT { (e)<-[:MENTIONS]-(:Chunk) }",
            chunk_id=chunk_id, eids=entity_ids,
        )

    def merge_relation(self, src_id: str, dst_id: str, predicate: str, description: str,
                       strength: int, chunk_id: str) -> None:
        self._run(
            "MATCH (a:Entity {entity_id: $src}), (b:Entity {entity_id: $dst}) "
            "MERGE (a)-[r:RELATED {predicate: $predicate}]->(b) "
            "ON CREATE SET r.evidence = [], r.strengths = [], r.weight = 0.0, r.description = $description "
            "WITH r WHERE NOT $chunk_id IN r.evidence "
            "SET r.evidence = r.evidence + $chunk_id, r.strengths = r.strengths + $strength, "
            "r.weight = r.weight + $strength, "
            "r.description = CASE WHEN size($description) > size(coalesce(r.description, '')) "
            "THEN $description ELSE r.description END",
            src=src_id, dst=dst_id, predicate=predicate, description=description,
            strength=strength, chunk_id=chunk_id,
        )

    def entity_context(self, entity_id: str, max_relations: int) -> EntityContext:
        rows = self._run(
            "MATCH (e:Entity {entity_id: $id}) "
            "OPTIONAL MATCH (e)-[r:RELATED]-(:Entity) "
            "WITH e, r ORDER BY r.weight DESC "
            "WITH e, collect(r)[..$limit] AS rels "
            f"RETURN e {{.*}} AS entity, [r IN rels | {_REL_MAP}] AS relations",
            id=entity_id, limit=max_relations,
        )
        if not rows:
            raise KeyError(entity_id)
        return EntityContext(
            entity=EntityRecord(**rows[0]["entity"]),
            relations=[RelationView(**r) for r in rows[0]["relations"]],
        )

    def fulltext_entities(self, query: str, k: int) -> list[tuple[str, float]]:
        # plain lower-cased word tokens: no Lucene operators or special characters survive
        tokens = [t.casefold() for t in _WORD.findall(query)]
        if not tokens:
            return []
        rows = self._run(
            "CALL db.index.fulltext.queryNodes('entity_names', $q) YIELD node, score "
            "RETURN node.entity_id AS id, score LIMIT $k",
            q=" ".join(tokens), k=k,
        )
        return [(r["id"], r["score"]) for r in rows]

    def neighborhood(self, seed_ids: list[str], hops: int, limit: int) -> list[RelationView]:
        hops = max(1, min(int(hops), 3))  # variable-length bounds cannot be parameters
        rows = self._run(
            "MATCH (s:Entity) WHERE s.entity_id IN $seeds "
            f"MATCH p = (s)-[:RELATED*1..{hops}]-(:Entity) "
            "WITH p LIMIT 5000 "
            "UNWIND relationships(p) AS r "
            "WITH DISTINCT r "
            "WITH r, startNode(r) AS a, endNode(r) AS b "
            "WITH r, a, b, COUNT { (a)--() } + COUNT { (b)--() } AS degree "
            "RETURN a.entity_id AS src_id, a.name AS src_name, r.predicate AS predicate, "
            "b.entity_id AS dst_id, b.name AS dst_name, r.description AS description, "
            "r.weight AS weight, r.evidence AS evidence, degree "
            "ORDER BY weight DESC, degree DESC LIMIT $limit",
            seeds=seed_ids, limit=limit,
        )
        return [RelationView(**r) for r in rows]

    # ---- wiki pages ----
    def upsert_wiki_page(self, page: WikiPageRecord, entity_id: str, links_to: list[str],
                         cites: list[str]) -> None:
        self._run(
            "MERGE (p:WikiPage {page_id: $page_id}) "
            "SET p.slug = $slug, p.title = $title, p.path = $path, p.content_hash = $content_hash, "
            "p.version = $version, p.updated_at = datetime() "
            "WITH p MATCH (e:Entity {entity_id: $entity_id}) MERGE (p)-[:ABOUT]->(e)",
            entity_id=entity_id, **page.model_dump(),
        )
        self._run(
            "MATCH (p:WikiPage {page_id: $page_id})-[r:LINKS_TO|CITES]->() DELETE r",
            page_id=page.page_id,
        )
        self._run(
            "MATCH (p:WikiPage {page_id: $page_id}) "
            "UNWIND $links AS target MATCH (q:WikiPage {page_id: target}) MERGE (p)-[:LINKS_TO]->(q)",
            page_id=page.page_id, links=links_to,
        )
        self._run(
            "MATCH (p:WikiPage {page_id: $page_id}) "
            "UNWIND $cites AS cid MATCH (c:Chunk {chunk_id: cid}) MERGE (p)-[:CITES]->(c)",
            page_id=page.page_id, cites=cites,
        )

    def wiki_page(self, page_id: str) -> WikiPageRecord | None:
        rows = self._run(f"MATCH (p:WikiPage {{page_id: $id}}) RETURN {_PAGE_FIELDS}", id=page_id)
        return WikiPageRecord(**rows[0]["page"]) if rows else None

    def page_by_slug(self, slug: str) -> WikiPageRecord | None:
        rows = self._run(f"MATCH (p:WikiPage {{slug: $slug}}) RETURN {_PAGE_FIELDS}", slug=slug)
        return WikiPageRecord(**rows[0]["page"]) if rows else None

    def wiki_pages(self) -> list[PageIndexRow]:
        rows = self._run(
            "MATCH (p:WikiPage)-[:ABOUT]->(e:Entity) "
            "RETURN p.page_id AS page_id, p.slug AS slug, p.title AS title, p.path AS path, "
            "e.type AS entity_type, coalesce(e.aliases, []) AS aliases "
            "ORDER BY entity_type, title"
        )
        return [PageIndexRow(**r) for r in rows]
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest -m integration tests/integration/test_graph_store.py -v`
Expected: 6 passed (first run pulls `neo4j:5.26-community`).

- [ ] **Step 6: Commit**

```bash
git add src/mnogobase/stores/graph_store.py tests/conftest.py tests/integration/test_graph_store.py
git commit -m "feat: neo4j graph store with cascade delete, fulltext and k-hop retrieval"
```

---

### Task 10: LLM entity/relation extractor

**Files:**
- Create: `src/mnogobase/extraction/__init__.py` (empty), `src/mnogobase/extraction/extractor.py`
- Test: `tests/unit/test_extractor.py`

**Interfaces:**
- Consumes: `LLMClient` + `templates.render` (Task 5); `Registry` (Task 4); `models.ChunkRecord`, `ExtractionResult`, `ExtractedEntity`, `ExtractedRelation` (Task 2); `ids.normalize_name` (Task 2).
- Produces: `PROMPT_VERSION = "extract-v1"`; `clean_extraction(result: ExtractionResult, entity_types: list[str]) -> ExtractionResult`; `Extractor(llm: LLMClient, registry: Registry, entity_types: list[str])` with `async extract(chunk: ChunkRecord, title: str) -> ExtractionResult`, `async extract_many(chunks: list[ChunkRecord], title: str) -> dict[str, ExtractionResult]` (failed chunks omitted), `cached(chunk: ChunkRecord) -> ExtractionResult | None`.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_extractor.py`:

```python
from mnogobase.config import DEFAULT_ENTITY_TYPES
from mnogobase.extraction.extractor import Extractor, clean_extraction
from mnogobase.models import ChunkRecord, ExtractedEntity, ExtractedRelation, ExtractionResult
from mnogobase.registry import Registry
from tests.fakes import FakeLLM, scripted_llm_handler

DOC = "a" * 16


def chunk(text: str, idx: int = 0) -> ChunkRecord:
    return ChunkRecord(chunk_id=f"{DOC}:{idx:05d}", doc_id=DOC, idx=idx, text=text,
                       context_text=text, headings=["Intro"])


def E(name, etype="Concept", aliases=()):  # noqa: N802
    return ExtractedEntity(name=name, type=etype, description=f"{name}.", aliases=list(aliases))


def R(source, target, predicate="uses", strength=5):  # noqa: N802
    return ExtractedRelation(source=source, target=target, predicate=predicate,
                             description="d", strength=strength)


async def test_extract_uses_cache(tmp_path):
    reg = Registry(tmp_path / "s.db")
    llm = FakeLLM(scripted_llm_handler)
    ex = Extractor(llm, reg, DEFAULT_ENTITY_TYPES)
    c = chunk("The Transformer uses softmax.")
    first = await ex.extract(c, "Doc")
    second = await ex.extract(c, "Doc")
    assert [e.name for e in first.entities] == ["Transformer", "Softmax"]
    assert second == first
    assert ex.cached(c) == first
    prompts = llm.calls_for("extract")
    assert len(prompts) == 1
    assert "Document: Doc" in prompts[0] and "Section: Intro" in prompts[0]
    assert "Person, Organization" in prompts[0]


def test_clean_extraction_relations():
    raw = ExtractionResult(
        entities=[
            E("Transformer", "method", aliases=["Трансформер"]),
            E("Softmax", "Weird"),
            E("   "),
            E("transformer", "Method", aliases=["TF"]),
        ],
        relations=[
            R("Трансформер", "Softmax", predicate="Uses It", strength=42),
            R("Transformer", "Unknown"),
            R("Softmax", "Softmax"),
            R("Transformer", "Softmax", predicate="uses it"),
        ],
    )
    out = clean_extraction(raw, DEFAULT_ENTITY_TYPES)
    assert [e.name for e in out.entities] == ["Transformer", "Softmax"]
    assert out.entities[0].type == "Method"
    assert out.entities[1].type == "Other"
    assert sorted(out.entities[0].aliases) == ["TF", "Трансформер"]
    assert len(out.relations) == 1
    rel = out.relations[0]
    assert (rel.source, rel.target, rel.predicate, rel.strength) == ("Transformer", "Softmax", "uses_it", 10)


async def test_extract_many_isolates_failures(tmp_path):
    reg = Registry(tmp_path / "s.db")

    def handler(task, prompt):
        if "BROKEN" in prompt:
            raise RuntimeError("llm down")
        return scripted_llm_handler(task, prompt)

    ex = Extractor(FakeLLM(handler), reg, DEFAULT_ENTITY_TYPES)
    ok, bad = chunk("Transformer text", 0), chunk("BROKEN", 1)
    results = await ex.extract_many([ok, bad], "Doc")
    assert list(results) == [ok.chunk_id]
    assert reg.chunk_extract_counts(DOC) == {"done": 1, "failed": 1}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/unit/test_extractor.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mnogobase.extraction'`.

- [ ] **Step 3: Implement `extractor.py`**

```python
from __future__ import annotations

import asyncio

from mnogobase.ids import normalize_name
from mnogobase.llm.client import LLMClient
from mnogobase.llm.templates import render
from mnogobase.log import get_logger
from mnogobase.models import ChunkRecord, ExtractedEntity, ExtractedRelation, ExtractionResult
from mnogobase.registry import Registry

PROMPT_VERSION = "extract-v1"


def clean_extraction(result: ExtractionResult, entity_types: list[str]) -> ExtractionResult:
    """Normalize types, merge duplicate names, resolve aliases in relations, drop bad edges."""
    allowed = {t.casefold(): t for t in entity_types}
    fallback = allowed.get("other", entity_types[-1])
    entities: dict[str, ExtractedEntity] = {}
    lookup: dict[str, str] = {}  # normalized name or alias -> entity key
    for e in result.entities:
        key = normalize_name(e.name)
        if not key:
            continue
        aliases = {a.strip() for a in e.aliases if a.strip() and normalize_name(a) != key}
        if key in entities:
            prev = entities[key]
            prev.aliases = sorted(set(prev.aliases) | aliases)
        else:
            entities[key] = ExtractedEntity(
                name=e.name.strip(),
                type=allowed.get(e.type.strip().casefold(), fallback),
                description=e.description.strip(),
                aliases=sorted(aliases),
            )
        lookup[key] = key
        for alias in aliases:
            lookup.setdefault(normalize_name(alias), key)

    relations: list[ExtractedRelation] = []
    seen: set[tuple[str, str, str]] = set()
    for r in result.relations:
        src = lookup.get(normalize_name(r.source))
        dst = lookup.get(normalize_name(r.target))
        if src is None or dst is None or src == dst:
            continue
        predicate = normalize_name(r.predicate).replace(" ", "_") or "related_to"
        if (src, dst, predicate) in seen:
            continue
        seen.add((src, dst, predicate))
        relations.append(ExtractedRelation(
            source=entities[src].name, target=entities[dst].name, predicate=predicate,
            description=r.description.strip(), strength=min(10, max(1, r.strength)),
        ))
    return ExtractionResult(entities=list(entities.values()), relations=relations)


class Extractor:
    def __init__(self, llm: LLMClient, registry: Registry, entity_types: list[str]):
        self._llm = llm
        self._registry = registry
        self._types = entity_types
        self._log = get_logger(__name__)

    @property
    def model(self) -> str:
        return self._llm.model_for("extract")

    def cached(self, chunk: ChunkRecord) -> ExtractionResult | None:
        raw = self._registry.get_extraction(chunk.chunk_id, PROMPT_VERSION, self.model)
        return ExtractionResult.model_validate_json(raw) if raw is not None else None

    async def extract(self, chunk: ChunkRecord, title: str) -> ExtractionResult:
        hit = self.cached(chunk)
        if hit is not None:
            return hit
        prompt = render(
            "extract",
            entity_types=", ".join(self._types),
            title=title,
            headings=" > ".join(chunk.headings) or "-",
            text=chunk.text,
        )
        raw = await self._llm.structured(
            [{"role": "user", "content": prompt}], ExtractionResult, task="extract"
        )
        result = clean_extraction(raw, self._types)
        self._registry.put_extraction(chunk.chunk_id, PROMPT_VERSION, self.model,
                                      result.model_dump_json())
        return result

    async def extract_many(self, chunks: list[ChunkRecord], title: str) -> dict[str, ExtractionResult]:
        async def one(chunk: ChunkRecord) -> tuple[str, ExtractionResult | None]:
            try:
                result = await self.extract(chunk, title)
            except Exception as exc:  # one bad chunk must not fail the document
                self._registry.set_chunk_extract(chunk.chunk_id, "failed", f"{type(exc).__name__}: {exc}")
                self._log.warning("extract_failed", chunk_id=chunk.chunk_id,
                                  error_type=type(exc).__name__, error=str(exc))
                return chunk.chunk_id, None
            self._registry.set_chunk_extract(chunk.chunk_id, "done")
            return chunk.chunk_id, result

        pairs = await asyncio.gather(*(one(c) for c in chunks))
        return {cid: r for cid, r in pairs if r is not None}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/unit/test_extractor.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mnogobase/extraction tests/unit/test_extractor.py
git commit -m "feat: LLM entity/relation extractor with caching and cleanup"
```

---

### Task 11: Entity resolution

**Files:**
- Create: `src/mnogobase/extraction/resolver.py`
- Test: `tests/integration/test_resolver.py`

**Interfaces:**
- Consumes: `GraphStore` (Task 9), `QdrantStore` (Task 8), `Embedder` (Task 6), `LLMClient` + `render` (Task 5), `config.ResolveSettings` (Task 1), `ids.entity_id`, `ids.normalize_name` (Task 2).
- Produces: `SameEntity(same: bool, reason: str)`, `MergedDescription(description: str)`; `EntityResolver(graph: GraphStore, vectors: QdrantStore, embedder: Embedder, llm: LLMClient, settings: ResolveSettings)` with `async resolve(entity: ExtractedEntity) -> EntityRecord` (persists the entity in Neo4j and in the Qdrant `entities` collection).

- [ ] **Step 1: Write the failing test**

`tests/integration/test_resolver.py`:

```python
import json

import pytest
from qdrant_client import QdrantClient

from mnogobase.config import ResolveSettings
from mnogobase.extraction.resolver import EntityResolver
from mnogobase.ids import entity_id
from mnogobase.models import ExtractedEntity
from mnogobase.stores.qdrant_store import QdrantStore
from tests.fakes import FakeLLM, scripted_llm_handler

pytestmark = pytest.mark.integration

VECTORS = {
    "Transformer": [1.0, 0.0, 0.0],
    "Transformer Architecture": [0.97, 0.243, 0.0],  # cosine 0.97 -> auto merge
    "Transformer Model": [0.85, 0.527, 0.0],         # cosine 0.85 -> ask the LLM
    "Cat": [0.0, 0.0, 1.0],
}


class MapEmbedder:
    model_id = "map"
    dim = 3

    def _vec(self, text: str) -> list[float]:
        return VECTORS[text.split(":")[0].strip()]

    def embed_documents(self, items):
        return [self._vec(i.text) for i in items]

    def embed_query(self, query):
        return self._vec(query)


def E(name, etype="Method", description="d", aliases=()):  # noqa: N802
    return ExtractedEntity(name=name, type=etype, description=description, aliases=list(aliases))


@pytest.fixture
def make_resolver(graph):
    def factory(handler=scripted_llm_handler, **settings):
        vectors = QdrantStore(QdrantClient(":memory:"), "t_", 3)
        vectors.ensure_collections()
        llm = FakeLLM(handler)
        resolver = EntityResolver(graph, vectors, MapEmbedder(), llm, ResolveSettings(**settings))
        return resolver, vectors, llm
    return factory


async def test_new_entity_is_persisted(graph, make_resolver):
    resolver, vectors, _ = make_resolver()
    rec = await resolver.resolve(E("Transformer", description="attention model"))
    assert rec.entity_id == entity_id("Method", "Transformer")
    assert graph.get_entity(rec.entity_id).description == "attention model"
    assert vectors.client.count(vectors.entities).count == 1


async def test_exact_id_merges_aliases_and_descriptions(graph, make_resolver):
    resolver, _, _ = make_resolver()
    first = await resolver.resolve(E("Transformer", description="one"))
    again = await resolver.resolve(E("transformer", description="two", aliases=["Трансформер"]))
    assert again.entity_id == first.entity_id
    stored = graph.get_entity(first.entity_id)
    assert stored.aliases == ["Трансформер"]
    assert stored.descriptions == ["one", "two"]


async def test_vector_auto_merge(graph, make_resolver):
    resolver, _, llm = make_resolver()
    base = await resolver.resolve(E("Transformer"))
    merged = await resolver.resolve(E("Transformer Architecture", etype="Concept"))
    assert merged.entity_id == base.entity_id
    assert "Transformer Architecture" in graph.get_entity(base.entity_id).aliases
    assert llm.calls_for("resolve") == []


@pytest.mark.parametrize("same", [True, False])
async def test_llm_check_band(graph, make_resolver, same):
    def handler(task, prompt):
        return json.dumps({"same": same, "reason": "r"})

    resolver, _, llm = make_resolver(handler)
    base = await resolver.resolve(E("Transformer"))
    other = await resolver.resolve(E("Transformer Model"))
    assert len(llm.calls_for("resolve")) == 1
    assert (other.entity_id == base.entity_id) is same


async def test_far_entity_is_new(graph, make_resolver):
    resolver, _, _ = make_resolver()
    a = await resolver.resolve(E("Transformer"))
    b = await resolver.resolve(E("Cat", etype="Concept"))
    assert a.entity_id != b.entity_id


async def test_descriptions_are_summarized(graph, make_resolver):
    resolver, _, llm = make_resolver(max_descriptions=2)
    for text in ("one", "two", "three"):
        rec = await resolver.resolve(E("Transformer", description=text))
    assert rec.description == "Merged description."
    assert graph.get_entity(rec.entity_id).descriptions == ["Merged description."]
    assert len(llm.calls_for("resolve")) == 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest -m integration tests/integration/test_resolver.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mnogobase.extraction.resolver'`.

- [ ] **Step 3: Implement `resolver.py`**

```python
from __future__ import annotations

from pydantic import BaseModel

from mnogobase.config import ResolveSettings
from mnogobase.embedding.base import Embedder
from mnogobase.ids import entity_id, normalize_name
from mnogobase.llm.client import LLMClient
from mnogobase.llm.templates import render
from mnogobase.log import get_logger
from mnogobase.models import EmbedInput, EntityRecord, ExtractedEntity
from mnogobase.stores.graph_store import GraphStore
from mnogobase.stores.qdrant_store import QdrantStore


class SameEntity(BaseModel):
    same: bool
    reason: str


class MergedDescription(BaseModel):
    description: str


class EntityResolver:
    """Maps an extracted entity onto an existing graph entity or creates a new one."""

    def __init__(self, graph: GraphStore, vectors: QdrantStore, embedder: Embedder,
                 llm: LLMClient, settings: ResolveSettings):
        self._graph = graph
        self._vectors = vectors
        self._embedder = embedder
        self._llm = llm
        self._s = settings
        self._resolved: dict[str, str] = {}  # extracted id -> canonical id (per process)
        self._log = get_logger(__name__)

    def _vector(self, name: str, description: str) -> list[float]:
        return self._embedder.embed_documents(
            [EmbedInput(text=f"{name}: {description}", title=name)]
        )[0]

    async def resolve(self, entity: ExtractedEntity) -> EntityRecord:
        key = entity_id(entity.type, entity.name)
        existing = self._graph.get_entity(self._resolved.get(key, key))
        if existing is None:
            existing = await self._match(entity)
        if existing is None:
            record = EntityRecord(
                entity_id=key, name=entity.name, type=entity.type,
                aliases=sorted(set(entity.aliases)), description=entity.description,
                descriptions=[entity.description] if entity.description else [],
            )
            changed = True
        else:
            record, changed = await self._merge(existing, entity)
        if changed:
            self._graph.upsert_entity(record)
            self._vectors.upsert_entities([record], [self._vector(record.name, record.description)])
        self._resolved[key] = record.entity_id
        return record

    async def _match(self, entity: ExtractedEntity) -> EntityRecord | None:
        hits = self._vectors.search_entities(
            self._vector(entity.name, entity.description), k=3, score_threshold=self._s.llm_check
        )
        for hit in hits:
            candidate = self._graph.get_entity(hit.key)
            if candidate is None:
                continue
            if hit.score >= self._s.auto_merge:
                self._log.info("entity_merged", name=entity.name, into=candidate.name,
                               score=round(hit.score, 3), method="vector")
                return candidate
            if await self._same(entity, candidate):
                self._log.info("entity_merged", name=entity.name, into=candidate.name,
                               score=round(hit.score, 3), method="llm")
                return candidate
        return None

    async def _same(self, entity: ExtractedEntity, candidate: EntityRecord) -> bool:
        prompt = render(
            "resolve_same",
            a_name=entity.name, a_type=entity.type, a_description=entity.description,
            b_name=candidate.name, b_type=candidate.type, b_description=candidate.description,
        )
        verdict = await self._llm.structured([{"role": "user", "content": prompt}], SameEntity,
                                             task="resolve")
        return verdict.same

    async def _merge(self, existing: EntityRecord, entity: ExtractedEntity) -> tuple[EntityRecord, bool]:
        aliases = set(existing.aliases) | set(entity.aliases)
        if normalize_name(entity.name) != normalize_name(existing.name):
            aliases.add(entity.name)
        descriptions = list(existing.descriptions)
        if entity.description and entity.description not in descriptions:
            descriptions.append(entity.description)
        changed = aliases != set(existing.aliases) or descriptions != existing.descriptions
        if not changed:
            return existing, False
        if len(descriptions) > self._s.max_descriptions:
            prompt = render("resolve_summarize", name=existing.name, type=existing.type,
                            descriptions="\n".join(f"- {d}" for d in descriptions))
            merged = await self._llm.structured([{"role": "user", "content": prompt}],
                                                MergedDescription, task="resolve")
            descriptions = [merged.description]
        return existing.model_copy(update={
            "aliases": sorted(aliases),
            "descriptions": descriptions,
            "description": " ".join(descriptions),
        }), True
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest -m integration tests/integration/test_resolver.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mnogobase/extraction/resolver.py tests/integration/test_resolver.py
git commit -m "feat: entity resolution by id, vector similarity and LLM check"
```

---

### Task 12: Wiki rendering and validation (pure functions)

**Files:**
- Create: `src/mnogobase/wiki/__init__.py` (empty), `src/mnogobase/wiki/validate.py`, `src/mnogobase/wiki/render.py`
- Test: `tests/unit/test_wiki_render.py`

**Interfaces:**
- Consumes: `models.EntityRecord`, `RelationView`, `PageIndexRow` (Task 2); `ids.normalize_name` (Task 2).
- Produces:
  - `validate.strip_reserved_sections(body: str) -> str` (drops a leading `# ` title, everything from `## Related`/`## Sources`, and footnote definition lines).
  - `validate.validate_citations(body: str, allowed: set[str]) -> tuple[str, list[str]]` (removes unknown `[^id]`, returns cited ids in first-seen order).
  - `validate.resolve_links(body: str, pages: dict[str, tuple[str, str, str]]) -> tuple[str, list[str]]` — `pages` maps `normalize_name(name or alias)` → `(entity_id, slug, title)`; known links become `[[slug|label]]`, unknown become plain `label`; returns linked entity ids.
  - `render.SourceRef(chunk_id: str, path: str | None, page: int | None)`; `render.render_page(entity, body, relations, linked: dict[str, str], sources: list[SourceRef], version: int, updated: date) -> str` (`linked` maps entity_id → slug); `render.parse_page(text) -> tuple[dict, str]`; `render.split_sections(text) -> list[tuple[str, str]]`; `render.render_index(rows: list[PageIndexRow]) -> str`; `render.append_log(path: Path, run_id: str, created: list[str], updated: list[str], deleted: list[str], now: datetime | None = None) -> None`.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_wiki_render.py`:

```python
from datetime import UTC, date, datetime

from mnogobase.models import EntityRecord, PageIndexRow, RelationView
from mnogobase.wiki.render import (
    SourceRef,
    append_log,
    parse_page,
    render_index,
    render_page,
    split_sections,
)
from mnogobase.wiki.validate import resolve_links, strip_reserved_sections, validate_citations


def test_validate_citations():
    body = "A [^x:1]. B [^bad]. C [^x:1][^x:2]."
    text, cited = validate_citations(body, {"x:1", "x:2"})
    assert text == "A [^x:1]. B. C [^x:1][^x:2]."
    assert cited == ["x:1", "x:2"]


def test_resolve_links():
    pages = {
        "softmax": ("e2", "softmax", "Softmax"),
        "трансформер": ("e1", "transformer", "Transformer"),
    }
    text, linked = resolve_links("See [[Softmax]], [[Трансформер|the model]] and [[Nope]].", pages)
    assert text == "See [[softmax|Softmax]], [[transformer|the model]] and Nope."
    assert linked == ["e2", "e1"]


def test_strip_reserved_sections():
    body = "# Title\nText [^a]\n\n## Details\nX\n\n## Related\n- foo\n\n## Sources\n[^a]: f"
    assert strip_reserved_sections(body) == "Text [^a]\n\n## Details\nX"
    assert strip_reserved_sections("Text\n[^a]: definition\nMore") == "Text\n\nMore"


def _page() -> str:
    entity = EntityRecord(entity_id="e1", name="Transformer", type="Method", aliases=["TF"])
    relations = [
        RelationView(src_id="e1", src_name="Transformer", predicate="related_to", dst_id="e2",
                     dst_name="Softmax"),
        RelationView(src_id="e3", src_name="Cat", predicate="uses", dst_id="e1",
                     dst_name="Transformer"),
    ]
    body = "Summary [^c1] about [[softmax|Softmax]].\n\n## Details\nMore [^c1]."
    return render_page(entity, body, relations, {"e2": "softmax"},
                       [SourceRef("c1", "/docs/a.pdf", 3)], 3, date(2026, 10, 7))


def test_render_and_parse_roundtrip():
    text = _page()
    assert text.startswith("---\nid: e1\n")
    assert "version: 3" in text and "updated: '2026-10-07'" in text
    assert "# Transformer" in text
    assert "- related_to → [[softmax|Softmax]]" in text
    assert "- uses ← Cat" in text
    assert "[^c1]: *a.pdf*, p.3" in text
    meta, body = parse_page(text)
    assert meta["version"] == 3 and meta["aliases"] == ["TF"]
    assert body == "Summary [^c1] about [[softmax|Softmax]].\n\n## Details\nMore [^c1]."


def test_split_sections_for_embedding():
    sections = split_sections(_page())
    names = [name for name, _ in sections]
    assert names == ["Summary", "Details", "Related"]
    summary = sections[0][1]
    assert summary.startswith("Transformer — Summary\n")
    assert "[^" not in summary and "about Softmax" in summary


def test_index_and_log(tmp_path):
    rows = [
        PageIndexRow(page_id="e2", slug="softmax", title="Softmax", path="entities/softmax.md",
                     entity_type="Concept"),
        PageIndexRow(page_id="e1", slug="transformer", title="Transformer",
                     path="entities/transformer.md", entity_type="Method"),
    ]
    index = render_index(rows)
    assert "## Concept\n- [[softmax|Softmax]]" in index
    assert "## Method\n- [[transformer|Transformer]]" in index
    log = tmp_path / "log.md"
    now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    append_log(log, "r1", ["Transformer"], [], [], now=now)
    append_log(log, "r2", [], ["Transformer"], ["Cat"], now=now)
    text = log.read_text(encoding="utf-8")
    assert text.startswith("# Wiki Log\n")
    assert "run r1" in text and "- created (1): Transformer" in text
    assert "- updated (1): Transformer" in text and "- deleted (1): Cat" in text
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/unit/test_wiki_render.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mnogobase.wiki'`.

- [ ] **Step 3: Implement `validate.py` and `render.py`**

`src/mnogobase/wiki/validate.py`:

```python
from __future__ import annotations

import re

from mnogobase.ids import normalize_name

_CITE = re.compile(r"\s?\[\^([^\]\s]+)\]")
_LINK = re.compile(r"\[\[([^\]|#]+)(?:\|([^\]]+))?\]\]")
_RESERVED = re.compile(r"^##\s+(Related|Sources)\b", re.IGNORECASE | re.MULTILINE)
_FOOTNOTE_DEF = re.compile(r"^\[\^[^\]]+\]:.*$", re.MULTILINE)


def strip_reserved_sections(body: str) -> str:
    body = body.strip()
    if body.startswith("# "):
        body = body.split("\n", 1)[1] if "\n" in body else ""
    match = _RESERVED.search(body)
    if match:
        body = body[: match.start()]
    body = _FOOTNOTE_DEF.sub("", body)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def validate_citations(body: str, allowed: set[str]) -> tuple[str, list[str]]:
    cited: list[str] = []

    def replace(match: re.Match) -> str:
        cid = match.group(1)
        if cid not in allowed:
            return ""
        if cid not in cited:
            cited.append(cid)
        return match.group(0)

    return _CITE.sub(replace, body), cited


def resolve_links(body: str, pages: dict[str, tuple[str, str, str]]) -> tuple[str, list[str]]:
    linked: list[str] = []

    def replace(match: re.Match) -> str:
        target = match.group(1).strip()
        label = (match.group(2) or target).strip()
        hit = pages.get(normalize_name(target))
        if hit is None:
            return label
        entity_id, slug, _title = hit
        if entity_id not in linked:
            linked.append(entity_id)
        return f"[[{slug}|{label}]]"

    return _LINK.sub(replace, body), linked
```

`src/mnogobase/wiki/render.py`:

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import groupby
from pathlib import Path

import yaml

from mnogobase.models import EntityRecord, PageIndexRow, RelationView
from mnogobase.wiki.validate import strip_reserved_sections

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_CITE_MARK = re.compile(r"\[\^[^\]]+\]")
_LINK_LABEL = re.compile(r"\[\[[^\]|]+\|([^\]]+)\]\]")


@dataclass
class SourceRef:
    chunk_id: str
    path: str | None
    page: int | None


def _link(entity_id: str, name: str, linked: dict[str, str]) -> str:
    slug = linked.get(entity_id)
    return f"[[{slug}|{name}]]" if slug else name


def render_page(entity: EntityRecord, body: str, relations: list[RelationView],
                linked: dict[str, str], sources: list[SourceRef], version: int,
                updated: date) -> str:
    front = {
        "id": entity.entity_id,
        "type": entity.type,
        "aliases": entity.aliases,
        "sources": len({s.path for s in sources if s.path}),
        "updated": updated.isoformat(),
        "version": version,
    }
    parts = ["---", yaml.safe_dump(front, allow_unicode=True, sort_keys=False).strip(), "---", "",
             f"# {entity.name}", "", body.strip(), ""]
    if relations:
        parts.append("## Related")
        for r in relations:
            if r.src_id == entity.entity_id:
                parts.append(f"- {r.predicate} → {_link(r.dst_id, r.dst_name, linked)}")
            else:
                parts.append(f"- {r.predicate} ← {_link(r.src_id, r.src_name, linked)}")
        parts.append("")
    if sources:
        parts.append("## Sources")
        for s in sources:
            where = f"*{Path(s.path).name}*" if s.path else "*unknown source*"
            if s.page:
                where += f", p.{s.page}"
            parts.append(f"[^{s.chunk_id}]: {where}")
        parts.append("")
    return "\n".join(parts)


def parse_page(text: str) -> tuple[dict, str]:
    meta: dict = {}
    match = _FRONTMATTER.match(text)
    if match:
        meta = yaml.safe_load(match.group(1)) or {}
        text = text[match.end():]
    return meta, strip_reserved_sections(text)


def split_sections(text: str) -> list[tuple[str, str]]:
    """Page → (section name, text to embed); citations stripped, Sources skipped."""
    match = _FRONTMATTER.match(text)
    body = text[match.end():] if match else text
    title = ""
    current = "Summary"
    buffer: list[str] = []
    raw: list[tuple[str, list[str]]] = []
    for line in body.splitlines():
        if line.startswith("# ") and not title:
            title = line[2:].strip()
            continue
        if line.startswith("## "):
            raw.append((current, buffer))
            current, buffer = line[3:].strip(), []
            continue
        buffer.append(line)
    raw.append((current, buffer))
    out: list[tuple[str, str]] = []
    for name, lines in raw:
        if name.casefold() == "sources":
            continue
        content = _LINK_LABEL.sub(r"\1", _CITE_MARK.sub("", "\n".join(lines))).strip()
        if content:
            out.append((name, f"{title} — {name}\n{content}" if title else content))
    return out


def render_index(rows: list[PageIndexRow]) -> str:
    lines = ["# Wiki Index", "", f"_{len(rows)} pages, generated automatically._", ""]
    ordered = sorted(rows, key=lambda r: (r.entity_type, r.title.casefold()))
    for entity_type, group in groupby(ordered, key=lambda r: r.entity_type):
        lines.append(f"## {entity_type}")
        lines.extend(f"- [[{r.slug}|{r.title}]]" for r in group)
        lines.append("")
    return "\n".join(lines)


def append_log(path: Path, run_id: str, created: list[str], updated: list[str],
               deleted: list[str], now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("# Wiki Log\n", encoding="utf-8")
    lines = ["", f"## {now:%Y-%m-%d %H:%M:%S} UTC · run {run_id or '-'}"]
    for label, names in (("created", created), ("updated", updated), ("deleted", deleted)):
        if names:
            lines.append(f"- {label} ({len(names)}): " + ", ".join(names))
    if len(lines) == 2:
        lines.append("- no changes")
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/unit/test_wiki_render.py -v`
Expected: 6 passed. If the `updated:` assertion fails because PyYAML emits the date unquoted, keep `front["updated"]` a string (as written) and adjust the assertion to the exact emitted form shown in the failure — the date must stay a string so `parse_page` round-trips it.

- [ ] **Step 5: Commit**

```bash
git add src/mnogobase/wiki tests/unit/test_wiki_render.py
git commit -m "feat: wiki page rendering, parsing and citation/link validation"
```

---

### Task 13: Wiki builder

**Files:**
- Create: `src/mnogobase/wiki/builder.py`
- Test: `tests/integration/test_wiki_builder.py`

**Interfaces:**
- Consumes: `GraphStore` (Task 9), `QdrantStore` (Task 8), `Embedder`/`SparseEncoder` (Task 6), `LLMClient`/`render` (Task 5), `Registry` (Task 4), wiki `render`/`validate` (Task 12), `config.WikiSettings`, `config.LANGUAGE_NAMES` (Task 1), `ids.slugify`, `ids.normalize_name` (Task 2).
- Produces: `WikiReport(created: list[str], updated: list[str], deleted: list[str], skipped: list[str])` (dataclass, names of entities); `WikiBuilder(settings: WikiSettings, graph, vectors, embedder, sparse, llm, registry)` with `async build(*, rebuild_all: bool = False, run_id: str = "", deleted: list[str] | None = None) -> WikiReport`. Files: `<wiki.dir>/entities/<slug>.md`, `<wiki.dir>/index.md`, `<wiki.dir>/log.md`.

- [ ] **Step 1: Write the failing test**

`tests/integration/test_wiki_builder.py`:

```python
from types import SimpleNamespace

import pytest
from qdrant_client import QdrantClient

from mnogobase.config import WikiSettings
from mnogobase.models import ChunkRecord, DocumentRecord, EmbedInput, EntityRecord
from mnogobase.registry import Registry
from mnogobase.stores.qdrant_store import QdrantStore
from mnogobase.wiki.builder import WikiBuilder
from tests.fakes import FakeEmbedder, FakeLLM, FakeSparse, scripted_llm_handler

pytestmark = pytest.mark.integration
DOC = "d" * 16


@pytest.fixture
def world(graph, tmp_path):
    def factory(min_mentions: int = 2):
        vectors = QdrantStore(QdrantClient(":memory:"), "t_", 64)
        vectors.ensure_collections()
        emb, sparse = FakeEmbedder(), FakeSparse()
        graph.upsert_document(DocumentRecord(doc_id=DOC, path="/docs/attention.pdf", title="T",
                                             mime="application/pdf"))
        texts = ["The Transformer uses attention.", "Softmax normalizes attention scores.",
                 "Transformer layers stack attention and softmax."]
        chunks = [ChunkRecord(chunk_id=f"{DOC}:{i:05d}", doc_id=DOC, idx=i, text=t,
                              context_text=t, page_start=i + 1, path="/docs/attention.pdf")
                  for i, t in enumerate(texts)]
        graph.upsert_chunks(chunks)
        vectors.upsert_chunks(chunks, emb.embed_documents([EmbedInput(text=t) for t in texts]),
                              sparse.encode_documents(texts))
        graph.upsert_entity(EntityRecord(entity_id="e1", name="Transformer", type="Method",
                                         description="Architecture.", descriptions=["Architecture."]))
        graph.upsert_entity(EntityRecord(entity_id="e2", name="Softmax", type="Concept",
                                         aliases=["софтмакс"], description="Function.",
                                         descriptions=["Function."]))
        for chunk, ids in ((chunks[0], ["e1"]), (chunks[1], ["e2"]), (chunks[2], ["e1", "e2"])):
            graph.add_mentions(chunk.chunk_id, ids)
            vectors.set_chunk_entities(chunk.chunk_id, ids)
        graph.merge_relation("e1", "e2", "uses", "Transformer uses softmax.", 5, chunks[2].chunk_id)
        registry = Registry(tmp_path / "state.db")
        registry.mark_dirty(["e1", "e2"])
        llm = FakeLLM(scripted_llm_handler)
        wiki_dir = tmp_path / "wiki"
        builder = WikiBuilder(WikiSettings(dir=wiki_dir, min_mentions=min_mentions), graph, vectors,
                              emb, sparse, llm, registry)
        return SimpleNamespace(builder=builder, vectors=vectors, registry=registry, llm=llm,
                               wiki=wiki_dir)
    return factory


async def test_build_creates_linked_cited_pages(world, graph):
    w = world()
    report = await w.builder.build(run_id="r1")
    assert sorted(report.created) == ["Softmax", "Transformer"]
    text = (w.wiki / "entities" / "transformer.md").read_text(encoding="utf-8")
    assert "# Transformer" in text
    assert "[[softmax|Softmax]]" in text
    assert "Nonexistent Thing" in text and "[[Nonexistent Thing]]" not in text
    assert "## Sources" in text and "*attention.pdf*, p." in text
    assert graph.wiki_page("e1").version == 1
    links = graph._run("MATCH (:WikiPage {page_id:'e1'})-[:LINKS_TO]->(p) RETURN p.page_id AS id")
    assert [r["id"] for r in links] == ["e2"]
    assert w.vectors.client.count(w.vectors.wiki).count >= 2
    assert "[[transformer|Transformer]]" in (w.wiki / "index.md").read_text(encoding="utf-8")
    assert "run r1" in (w.wiki / "log.md").read_text(encoding="utf-8")
    assert w.registry.dirty() == []


async def test_rebuild_updates_existing_page(world, graph):
    w = world()
    await w.builder.build(run_id="r1")
    w.registry.mark_dirty(["e1"])
    report = await w.builder.build(run_id="r2")
    assert report.updated == ["Transformer"] and report.created == []
    assert graph.wiki_page("e1").version == 2
    last_prompt = [p for p in w.llm.calls_for("wiki") if '"Transformer"' in p][-1]
    assert "Summary sentence." in last_prompt  # existing body was passed back to the LLM


async def test_entities_below_threshold_are_skipped(world):
    w = world(min_mentions=3)
    report = await w.builder.build()
    assert report.created == [] and report.updated == []
    assert w.registry.dirty() == []
    assert not (w.wiki / "entities").exists()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest -m integration tests/integration/test_wiki_builder.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mnogobase.wiki.builder'`.

- [ ] **Step 3: Implement `builder.py`**

```python
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from mnogobase.config import LANGUAGE_NAMES, WikiSettings
from mnogobase.embedding.base import Embedder, SparseEncoder
from mnogobase.ids import normalize_name, slugify
from mnogobase.llm.client import LLMClient
from mnogobase.llm.templates import render
from mnogobase.log import get_logger
from mnogobase.models import EmbedInput, EntityRecord, SearchHit, WikiPageRecord
from mnogobase.registry import Registry
from mnogobase.stores.graph_store import GraphStore
from mnogobase.stores.qdrant_store import QdrantStore
from mnogobase.wiki.render import (
    SourceRef,
    append_log,
    parse_page,
    render_index,
    render_page,
    split_sections,
)
from mnogobase.wiki.validate import resolve_links, strip_reserved_sections, validate_citations

PageLookup = dict[str, tuple[str, str, str]]  # normalized name/alias -> (entity_id, slug, title)


@dataclass
class WikiReport:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


@dataclass
class _Built:
    entity: EntityRecord
    record: WikiPageRecord
    links_to: list[str]
    cited: list[str]
    created: bool


def _evidence_line(hit: SearchHit) -> str:
    where = Path(hit.payload.get("path") or "unknown").name
    if hit.payload.get("page"):
        where += f", p.{hit.payload['page']}"
    return f"[{hit.key}] ({where})\n{hit.payload.get('text', '')}"


class WikiBuilder:
    def __init__(self, settings: WikiSettings, graph: GraphStore, vectors: QdrantStore,
                 embedder: Embedder, sparse: SparseEncoder, llm: LLMClient, registry: Registry):
        self._s = settings
        self._graph = graph
        self._vectors = vectors
        self._embedder = embedder
        self._sparse = sparse
        self._llm = llm
        self._registry = registry
        self._log = get_logger(__name__)

    async def build(self, *, rebuild_all: bool = False, run_id: str = "",
                    deleted: list[str] | None = None) -> WikiReport:
        dirty = set(self._registry.dirty())
        candidates = [e for e in self._graph.entities(min_mentions=self._s.min_mentions)
                      if rebuild_all or e.entity_id in dirty]
        report = WikiReport(deleted=list(deleted or []))
        slugs = self._assign_slugs(candidates)
        lookup = self._page_lookup(candidates, slugs)
        results = await asyncio.gather(
            *(self._build_one(e, slugs[e.entity_id], lookup) for e in candidates)
        )
        built = [b for b in results if b is not None]
        # two passes so LINKS_TO can target pages created in this same run
        for b in built:
            self._graph.upsert_wiki_page(b.record, b.entity.entity_id, [], b.cited)
        for b in built:
            self._graph.upsert_wiki_page(b.record, b.entity.entity_id, b.links_to, b.cited)
            (report.created if b.created else report.updated).append(b.entity.name)
        built_ids = {b.entity.entity_id for b in built}
        report.skipped = [e.name for e in candidates if e.entity_id not in built_ids]
        self._registry.clear_dirty(dirty)
        self._s.dir.mkdir(parents=True, exist_ok=True)
        (self._s.dir / "index.md").write_text(render_index(self._graph.wiki_pages()), encoding="utf-8")
        if report.created or report.updated or report.deleted:
            append_log(self._s.dir / "log.md", run_id, sorted(report.created),
                       sorted(report.updated), sorted(report.deleted))
        self._log.info("wiki_built", created=len(report.created), updated=len(report.updated),
                       deleted=len(report.deleted), skipped=len(report.skipped))
        return report

    def _assign_slugs(self, candidates: list[EntityRecord]) -> dict[str, str]:
        taken = {row.slug: row.page_id for row in self._graph.wiki_pages()}
        slugs: dict[str, str] = {}
        for entity in candidates:
            page = self._graph.wiki_page(entity.entity_id)
            if page is not None:
                slugs[entity.entity_id] = page.slug
        for entity in candidates:
            if entity.entity_id in slugs:
                continue
            base = slugify(entity.name)
            options = (base, f"{base}-{slugify(entity.type)}", f"{base}-{entity.entity_id[:6]}")
            slug = next(o for o in options if taken.get(o, entity.entity_id) == entity.entity_id)
            taken[slug] = entity.entity_id
            slugs[entity.entity_id] = slug
        return slugs

    def _page_lookup(self, candidates: list[EntityRecord], slugs: dict[str, str]) -> PageLookup:
        lookup: PageLookup = {}
        for row in self._graph.wiki_pages():
            for name in (row.title, *row.aliases):
                lookup.setdefault(normalize_name(name), (row.page_id, row.slug, row.title))
        for entity in candidates:
            for name in (entity.name, *entity.aliases):
                lookup.setdefault(normalize_name(name),
                                  (entity.entity_id, slugs[entity.entity_id], entity.name))
        return lookup

    async def _build_one(self, entity: EntityRecord, slug: str, lookup: PageLookup) -> _Built | None:
        context = self._graph.entity_context(entity.entity_id, max_relations=30)
        query = self._embedder.embed_query(f"{entity.name}: {entity.description}")
        hits = self._vectors.search_chunks_for_entity(query, entity.entity_id, self._s.evidence_k)
        if not hits:
            return None
        rel_path = f"entities/{slug}.md"
        file = self._s.dir / rel_path
        previous = self._graph.wiki_page(entity.entity_id)
        existing_body = parse_page(file.read_text(encoding="utf-8"))[1] if file.exists() else ""
        prompt = render(
            "wiki_page",
            language=LANGUAGE_NAMES.get(self._s.language, self._s.language),
            name=entity.name,
            type=entity.type,
            description=entity.description or "-",
            aliases=", ".join(entity.aliases) or "-",
            relations="\n".join(f"- {r.src_name} {r.predicate} {r.dst_name}: {r.description}"
                                for r in context.relations) or "-",
            evidence="\n\n".join(_evidence_line(h) for h in hits),
            example_id=hits[0].key,
            existing=existing_body,
        )
        body = await self._llm.complete([{"role": "user", "content": prompt}], task="wiki")
        body = strip_reserved_sections(body)
        body, cited = validate_citations(body, {h.key for h in hits})
        body, linked_ids = resolve_links(body, lookup)

        page_slugs = {eid: s for eid, s, _ in lookup.values()}
        neighbours = {r.src_id for r in context.relations} | {r.dst_id for r in context.relations}
        linked = {eid: page_slugs[eid] for eid in neighbours
                  if eid in page_slugs and eid != entity.entity_id}
        payloads = {h.key: h.payload for h in hits}
        sources = [SourceRef(c, payloads[c].get("path"), payloads[c].get("page")) for c in cited]
        version = previous.version + 1 if previous else 1
        text = render_page(entity, body, context.relations, linked, sources, version, date.today())
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding="utf-8")

        sections = split_sections(text)
        texts = [t for _, t in sections]
        self._vectors.upsert_wiki_sections(
            entity.entity_id, entity.entity_id, rel_path, sections,
            self._embedder.embed_documents([EmbedInput(text=t, title=entity.name) for t in texts]),
            self._sparse.encode_documents(texts),
        )
        record = WikiPageRecord(
            page_id=entity.entity_id, slug=slug, title=entity.name, path=rel_path,
            content_hash=hashlib.sha1(text.encode("utf-8")).hexdigest(), version=version,
        )
        links_to = sorted((set(linked_ids) | set(linked)) - {entity.entity_id})
        return _Built(entity, record, links_to, cited, created=previous is None)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest -m integration tests/integration/test_wiki_builder.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mnogobase/wiki/builder.py tests/integration/test_wiki_builder.py
git commit -m "feat: incremental wiki builder with citations, links, index and log"
```

---

### Task 14: Ingest pipeline and application wiring

**Files:**
- Create: `src/mnogobase/pipeline.py`, `src/mnogobase/app.py`
- Test: `tests/integration/test_pipeline.py`

**Interfaces:**
- Consumes: everything from Tasks 1–13.
- Produces:
  - `pipeline.EmbedderMismatchError(RuntimeError)`, `pipeline.ExtractionFailedError(RuntimeError)`.
  - `pipeline.IngestReport(processed: list[str], skipped: list[str], failed: dict[str, str], wiki: WikiReport | None)`.
  - `pipeline.Pipeline(settings, registry, parser, chunker, embedder, sparse, vectors, graph, extractor, resolver, wiki)` with `prepare(check_embedder: bool = True, resume: bool = True) -> None` (`resume=False` for read-only commands so they never touch stages of a concurrent ingest), `discover(paths: Sequence[Path]) -> list[Path]`, `async ingest(paths: Sequence[Path], *, build_wiki: bool = True, retry_failed: bool = False, run_id: str = "") -> IngestReport`, `load_chunks(doc_id: str) -> tuple[DocumentRecord, list[ChunkRecord]]`, `chunks_path(doc_id: str) -> Path`.
  - `app.App` dataclass (`settings, device, registry, embedder, sparse, vectors, graph, llm, parser, chunker, extractor, resolver, wiki, pipeline`, method `close()`); `app.build_app(settings: Settings, *, embedder=None, sparse=None, llm=None, vectors=None, graph=None) -> App`.

- [ ] **Step 1: Write the failing test**

`tests/integration/test_pipeline.py`:

```python
import shutil
from pathlib import Path

import pytest
from qdrant_client import QdrantClient
from qdrant_client import models as qm

from mnogobase.app import build_app
from mnogobase.config import ChunkingSettings, WikiSettings, load_settings
from mnogobase.ids import entity_id, file_doc_id
from mnogobase.pipeline import EmbedderMismatchError
from mnogobase.stores.qdrant_store import QdrantStore
from tests.fakes import FakeEmbedder, FakeLLM, FakeSparse, scripted_llm_handler

pytestmark = pytest.mark.integration
FIXTURES = Path(__file__).parents[1] / "fixtures"


@pytest.fixture
def docs(tmp_path) -> Path:
    folder = tmp_path / "docs"
    folder.mkdir()
    for name in ("attention_en.md", "vnimanie_ru.md"):
        shutil.copy(FIXTURES / name, folder / name)
    return folder


@pytest.fixture
def make_app(graph, tmp_path):
    apps = []

    def factory(handler=scripted_llm_handler):
        settings = load_settings(tmp_path / "missing.yaml").model_copy(update={
            "data_dir": tmp_path / ".mb",
            "logs_dir": tmp_path / "logs",
            "wiki": WikiSettings(dir=tmp_path / "wiki", min_mentions=1),
            "chunking": ChunkingSettings(max_tokens=128),
        })
        vectors = QdrantStore(QdrantClient(":memory:"), "t_", 64)
        app = build_app(settings, embedder=FakeEmbedder(), sparse=FakeSparse(),
                        llm=FakeLLM(handler), vectors=vectors, graph=graph)
        apps.append(app)
        return app

    yield factory
    for app in apps:
        app.registry.close()


def chunk_count(app, doc_id: str) -> int:
    flt = qm.Filter(must=[qm.FieldCondition(key="doc_id", match=qm.MatchValue(value=doc_id))])
    return app.vectors.client.count(app.vectors.chunks, count_filter=flt).count


async def test_ingest_builds_vectors_graph_and_wiki(make_app, docs, tmp_path):
    app = make_app()
    report = await app.pipeline.ingest([docs])
    assert report.failed == {}
    assert len(report.processed) == 2
    assert app.graph.counts()["Document"] == 2
    attention = app.graph.get_entity(entity_id("Method", "Attention Mechanism"))
    assert attention is not None and attention.mention_count >= 2  # RU and EN merged
    assert "механизм внимания" in attention.aliases
    assert (tmp_path / "wiki" / "entities" / "attention-mechanism.md").exists()
    assert report.wiki is not None and "Attention Mechanism" in report.wiki.created
    hits = app.vectors.search_chunks_for_entity(app.embedder.embed_query("attention"),
                                                attention.entity_id, k=10)
    assert hits


async def test_reingest_is_noop(make_app, docs):
    app = make_app()
    await app.pipeline.ingest([docs])
    calls = len(app.llm.calls_for("extract"))
    report = await app.pipeline.ingest([docs])
    assert report.processed == [] and len(report.skipped) == 2
    assert len(app.llm.calls_for("extract")) == calls


async def test_changed_file_replaces_old_chunks(make_app, docs):
    app = make_app()
    await app.pipeline.ingest([docs])
    path = docs / "attention_en.md"
    old = file_doc_id(path)
    path.write_text(path.read_text(encoding="utf-8") + "\n\n## Extra\n\nSoftmax again.\n",
                    encoding="utf-8")
    report = await app.pipeline.ingest([docs])
    assert report.processed == [str(path.resolve())]
    assert chunk_count(app, old) == 0
    assert chunk_count(app, file_doc_id(path)) > 0
    assert app.graph.counts()["Document"] == 2


async def test_duplicate_content_survives_change(make_app, docs):
    app = make_app()
    copy = docs / "copy.md"
    shutil.copy(docs / "attention_en.md", copy)
    await app.pipeline.ingest([docs])
    shared = file_doc_id(copy)
    copy.write_text("# Different\n\nSomething about softmax.\n", encoding="utf-8")
    report = await app.pipeline.ingest([docs])
    assert report.failed == {}
    assert chunk_count(app, shared) > 0  # still referenced by attention_en.md
    assert app.graph.counts()["Document"] == 3


async def test_empty_document(make_app, tmp_path):
    folder = tmp_path / "empty_docs"
    folder.mkdir()
    (folder / "empty.md").write_text("", encoding="utf-8")
    app = make_app()
    report = await app.pipeline.ingest([folder])
    assert report.failed == {}
    assert len(report.processed) == 1
    assert app.graph.counts()["Chunk"] == 0


async def test_extraction_failure_then_retry(make_app, docs):
    def broken(task, prompt):
        if task == "extract":
            raise RuntimeError("llm down")
        return scripted_llm_handler(task, prompt)

    app = make_app(broken)
    report = await app.pipeline.ingest([docs], build_wiki=False)
    assert len(report.failed) == 2
    assert all("ExtractionFailedError" in msg for msg in report.failed.values())
    doc_id = file_doc_id(docs / "attention_en.md")
    assert app.registry.stage_status(doc_id, "extract") == "failed"
    calls = len(app.llm.calls_for("extract"))

    again = await app.pipeline.ingest([docs], build_wiki=False)
    assert len(again.failed) == 2 and "--retry-failed" in next(iter(again.failed.values()))
    assert len(app.llm.calls_for("extract")) == calls

    app.llm.handler = scripted_llm_handler
    fixed = await app.pipeline.ingest([docs], build_wiki=False, retry_failed=True)
    assert fixed.failed == {} and len(fixed.processed) == 2


async def test_graph_stage_rerun_is_idempotent(make_app, docs):
    app = make_app()
    await app.pipeline.ingest([docs], build_wiki=False)
    before = app.graph.counts()
    doc_id = file_doc_id(docs / "attention_en.md")
    app.registry.set_stage(doc_id, "graph", "running")  # simulate a crash during the graph stage
    report = await app.pipeline.ingest([docs], build_wiki=False)
    assert report.processed == [str((docs / "attention_en.md").resolve())]
    assert app.graph.counts() == before


async def test_embedder_mismatch_is_refused(make_app):
    app = make_app()
    app.registry.set_meta("embedder", "other-model:1024")
    with pytest.raises(EmbedderMismatchError, match="reindex"):
        app.pipeline.prepare()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest -m integration tests/integration/test_pipeline.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mnogobase.app'`.

- [ ] **Step 3: Implement `pipeline.py`**

```python
from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from mnogobase.chunking.hybrid import Chunker
from mnogobase.config import Settings
from mnogobase.embedding.base import Embedder, SparseEncoder, embedder_signature
from mnogobase.extraction.extractor import Extractor
from mnogobase.extraction.resolver import EntityResolver
from mnogobase.ids import file_doc_id, normalize_name
from mnogobase.log import get_logger, log_stage
from mnogobase.models import ChunkRecord, DocumentRecord, EmbedInput
from mnogobase.parsing.docling_parser import DoclingParser
from mnogobase.registry import Registry
from mnogobase.stores.graph_store import GraphStore
from mnogobase.stores.qdrant_store import QdrantStore
from mnogobase.wiki.builder import WikiBuilder, WikiReport


class EmbedderMismatchError(RuntimeError):
    pass


class ExtractionFailedError(RuntimeError):
    pass


@dataclass
class IngestReport:
    processed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    wiki: WikiReport | None = None


class _PreviouslyFailed(Exception):
    pass


class Pipeline:
    def __init__(self, settings: Settings, registry: Registry, parser: DoclingParser,
                 chunker: Chunker, embedder: Embedder, sparse: SparseEncoder,
                 vectors: QdrantStore, graph: GraphStore, extractor: Extractor,
                 resolver: EntityResolver, wiki: WikiBuilder):
        self._s = settings
        self._registry = registry
        self._parser = parser
        self._chunker = chunker
        self._embedder = embedder
        self._sparse = sparse
        self._vectors = vectors
        self._graph = graph
        self._extractor = extractor
        self._resolver = resolver
        self._wiki = wiki
        self._log = get_logger(__name__)

    # ---- setup ----
    def prepare(self, check_embedder: bool = True, resume: bool = True) -> None:
        signature = embedder_signature(self._embedder)
        stored = self._registry.get_meta("embedder")
        if check_embedder and stored is not None and stored != signature:
            raise EmbedderMismatchError(
                f"index was built with {stored}, config now uses {signature}; run `mnogobase reindex`"
            )
        self._vectors.ensure_collections()
        self._graph.ensure_schema()
        if stored is None:
            self._registry.set_meta("embedder", signature)
        if resume:
            resumed = self._registry.reset_running()
            if resumed:
                self._log.warning("resuming_interrupted_stages", count=resumed)

    def discover(self, paths: Sequence[Path]) -> list[Path]:
        exts = {f".{e.lower().lstrip('.')}" for e in self._s.parsing.extensions}
        found: list[Path] = []
        for path in paths:
            path = Path(path)
            if path.is_dir():
                for file in sorted(path.rglob("*")):
                    hidden = any(part.startswith(".") for part in file.relative_to(path).parts)
                    if file.is_file() and file.suffix.lower() in exts and not hidden:
                        found.append(file)
            elif path.is_file() and path.suffix.lower() in exts:
                found.append(path)
        return list(dict.fromkeys(f.resolve() for f in found))

    # ---- chunk cache ----
    def chunks_path(self, doc_id: str) -> Path:
        return self._s.data_dir / "cache" / f"{doc_id}.chunks.json"

    def _save_chunks(self, doc: DocumentRecord, chunks: list[ChunkRecord]) -> None:
        path = self.chunks_path(doc.doc_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"document": doc.model_dump(), "chunks": [c.model_dump() for c in chunks]}
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def load_chunks(self, doc_id: str) -> tuple[DocumentRecord, list[ChunkRecord]]:
        payload = json.loads(self.chunks_path(doc_id).read_text(encoding="utf-8"))
        return (DocumentRecord(**payload["document"]),
                [ChunkRecord(**c) for c in payload["chunks"]])

    # ---- ingest ----
    async def ingest(self, paths: Sequence[Path], *, build_wiki: bool = True,
                     retry_failed: bool = False, run_id: str = "") -> IngestReport:
        self.prepare()
        report = IngestReport()
        removed_names: list[str] = []
        for path in self.discover(paths):
            key = str(path)
            try:
                status = await self._ingest_file(path, retry_failed, removed_names)
            except _PreviouslyFailed as exc:
                report.failed[key] = str(exc)
                continue
            except Exception as exc:
                report.failed[key] = f"{type(exc).__name__}: {exc}"
                continue
            (report.processed if status == "processed" else report.skipped).append(key)
        if build_wiki:
            report.wiki = await self._wiki.build(run_id=run_id, deleted=removed_names)
        self._log.info("ingest_done", processed=len(report.processed),
                       skipped=len(report.skipped), failed=len(report.failed))
        return report

    async def _ingest_file(self, path: Path, retry_failed: bool, removed_names: list[str]) -> str:
        key = str(path)
        doc_id = file_doc_id(path)
        stat = path.stat()
        row = self._registry.get_file(key)
        if row is not None and row.doc_id != doc_id:
            removed_names.extend(self._remove_doc(row.doc_id, keep_path=key))
        pending = self._registry.pending_stages(doc_id)
        if not pending:
            self._registry.upsert_file(key, doc_id, stat.st_size, stat.st_mtime, "done")
            return "skipped"
        failed_stage = next((s for s in pending if self._registry.stage_status(doc_id, s) == "failed"), None)
        if failed_stage and not retry_failed:
            self._registry.upsert_file(key, doc_id, stat.st_size, stat.st_mtime, "failed")
            raise _PreviouslyFailed(
                f"stage {failed_stage} failed earlier; rerun with --retry-failed"
            )
        self._registry.upsert_file(key, doc_id, stat.st_size, stat.st_mtime, "processing")
        structlog.contextvars.bind_contextvars(doc_id=doc_id)
        try:
            for stage in pending:
                self._registry.set_stage(doc_id, stage, "running")
                try:
                    with log_stage(self._log, stage, path=key):
                        await self._run_stage(stage, path, doc_id)
                except Exception as exc:
                    self._registry.set_stage(doc_id, stage, "failed", error=f"{type(exc).__name__}: {exc}")
                    self._registry.set_file_status(key, "failed")
                    raise
                self._registry.set_stage(doc_id, stage, "done")
        finally:
            structlog.contextvars.unbind_contextvars("doc_id")
        self._registry.set_file_status(key, "done")
        return "processed"

    def _remove_doc(self, doc_id: str, keep_path: str) -> list[str]:
        """Delete an old document version unless another path still has the same content."""
        if any(p != keep_path for p in self._registry.paths_for_doc(doc_id)):
            return []
        result = self._graph.delete_document(doc_id)
        self._vectors.delete_doc(doc_id)
        self._vectors.delete_entities(result.removed_entity_ids)
        for page_id, rel_path in result.removed_pages:
            self._vectors.delete_wiki_page(page_id)
            (self._s.wiki.dir / rel_path).unlink(missing_ok=True)
        self._registry.mark_dirty(set(result.affected) - set(result.removed_entity_ids))
        self._registry.clear_dirty(result.removed_entity_ids)
        self._registry.clear_doc(doc_id)
        self._parser.drop_cache(doc_id)
        self.chunks_path(doc_id).unlink(missing_ok=True)
        self._log.info("document_removed", old_doc_id=doc_id, removed_entities=len(result.removed_entity_ids))
        return result.removed_names

    async def _run_stage(self, stage: str, path: Path, doc_id: str) -> None:
        if stage == "parse":
            self._parser.parse(path, doc_id)
        elif stage == "chunk":
            parsed = self._parser.load(doc_id, path)
            chunks = self._chunker.chunk(parsed, doc_id, str(path))
            doc = DocumentRecord(doc_id=doc_id, path=str(path), title=parsed.title,
                                 mime=parsed.mime, n_pages=parsed.n_pages)
            self._save_chunks(doc, chunks)
        elif stage == "embed":
            self._embed(doc_id)
        elif stage == "extract":
            await self._extract(doc_id)
        elif stage == "graph":
            await self._build_graph(doc_id)
        else:
            raise ValueError(f"unknown stage {stage}")

    def _embed(self, doc_id: str) -> None:
        doc, chunks = self.load_chunks(doc_id)
        self._graph.upsert_document(doc)
        if not chunks:
            return
        texts = [c.context_text for c in chunks]
        dense = self._embedder.embed_documents([EmbedInput(text=t, title=doc.title) for t in texts])
        self._vectors.upsert_chunks(chunks, dense, self._sparse.encode_documents(texts))
        self._graph.upsert_chunks(chunks)

    async def _extract(self, doc_id: str) -> None:
        doc, chunks = self.load_chunks(doc_id)
        if not chunks:
            return
        results = await self._extractor.extract_many(chunks, doc.title)
        failed = len(chunks) - len(results)
        if failed / len(chunks) > self._s.extract.max_failed_ratio:
            raise ExtractionFailedError(f"{failed}/{len(chunks)} chunks failed extraction")

    async def _build_graph(self, doc_id: str) -> None:
        _doc, chunks = self.load_chunks(doc_id)
        touched: set[str] = set()
        for chunk in chunks:
            result = self._extractor.cached(chunk)
            if result is None:
                continue
            ids_by_name: dict[str, str] = {}
            for extracted in result.entities:
                record = await self._resolver.resolve(extracted)
                ids_by_name[normalize_name(extracted.name)] = record.entity_id
                for alias in extracted.aliases:
                    ids_by_name.setdefault(normalize_name(alias), record.entity_id)
            entity_ids = sorted(set(ids_by_name.values()))
            self._graph.add_mentions(chunk.chunk_id, entity_ids)
            self._vectors.set_chunk_entities(chunk.chunk_id, entity_ids)
            for rel in result.relations:
                src = ids_by_name.get(normalize_name(rel.source))
                dst = ids_by_name.get(normalize_name(rel.target))
                if src and dst and src != dst:
                    self._graph.merge_relation(src, dst, rel.predicate, rel.description,
                                               rel.strength, chunk.chunk_id)
            touched.update(entity_ids)
        self._registry.mark_dirty(touched)
```

- [ ] **Step 4: Implement `app.py`**

```python
from __future__ import annotations

from dataclasses import dataclass

from mnogobase.chunking.hybrid import Chunker
from mnogobase.config import Settings
from mnogobase.device import detect_device
from mnogobase.embedding.base import Embedder, SparseEncoder
from mnogobase.embedding.ollama import OllamaEmbedder
from mnogobase.embedding.sparse import BM25Encoder
from mnogobase.extraction.extractor import Extractor
from mnogobase.extraction.resolver import EntityResolver
from mnogobase.llm.client import LLMClient, OpenAICompatLLM
from mnogobase.parsing.docling_parser import DoclingParser
from mnogobase.pipeline import Pipeline
from mnogobase.registry import Registry
from mnogobase.stores.graph_store import GraphStore
from mnogobase.stores.qdrant_store import QdrantStore
from mnogobase.wiki.builder import WikiBuilder


@dataclass
class App:
    settings: Settings
    device: str
    registry: Registry
    embedder: Embedder
    sparse: SparseEncoder
    vectors: QdrantStore
    graph: GraphStore
    llm: LLMClient
    parser: DoclingParser
    chunker: Chunker
    extractor: Extractor
    resolver: EntityResolver
    wiki: WikiBuilder
    pipeline: Pipeline

    def close(self) -> None:
        self.graph.close()
        self.registry.close()


def build_app(settings: Settings, *, embedder: Embedder | None = None,
              sparse: SparseEncoder | None = None, llm: LLMClient | None = None,
              vectors: QdrantStore | None = None, graph: GraphStore | None = None) -> App:
    device = detect_device(settings.device)
    registry = Registry(settings.data_dir / "state.db")
    embedder = embedder or OllamaEmbedder(settings.embedder)
    sparse = sparse or BM25Encoder(settings.sparse.model, device=device)
    vectors = vectors or QdrantStore.from_settings(settings.qdrant, embedder.dim)
    graph = graph or GraphStore.from_settings(settings.neo4j)
    llm = llm or OpenAICompatLLM(settings.llm)
    parser = DoclingParser(settings.parsing, device, settings.data_dir / "cache")
    chunker = Chunker(settings.chunking)
    extractor = Extractor(llm, registry, settings.extract.entity_types)
    resolver = EntityResolver(graph, vectors, embedder, llm, settings.resolve)
    wiki = WikiBuilder(settings.wiki, graph, vectors, embedder, sparse, llm, registry)
    pipeline = Pipeline(settings, registry, parser, chunker, embedder, sparse, vectors, graph,
                        extractor, resolver, wiki)
    return App(settings, device, registry, embedder, sparse, vectors, graph, llm, parser,
               chunker, extractor, resolver, wiki, pipeline)
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest -m integration tests/integration/test_pipeline.py -v`
Expected: 8 passed.

- [ ] **Step 6: Run the whole suite**

Run: `uv run pytest && uv run pytest -m integration`
Expected: all unit tests pass, then all integration tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/mnogobase/pipeline.py src/mnogobase/app.py tests/integration/test_pipeline.py
git commit -m "feat: resumable ingest pipeline with cascade replace and app wiring"
```

---

### Task 15: Retrieval modes, answer generation, comparison

**Files:**
- Create: `src/mnogobase/retrieval/__init__.py`, `base.py`, `rag.py`, `wiki.py`, `graph.py`, `combined.py`, `answer.py`, `compare.py`
- Test: `tests/unit/test_retrieval.py`, `tests/integration/test_graph_retriever.py`

**Interfaces:**
- Consumes: `QdrantStore` (Task 8), `GraphStore` (Task 9), `Embedder`/`SparseEncoder` (Task 6), `LLMClient`/`render` (Task 5), `models.ContextItem`, `Source`, `Answer` (Task 2), `config.Settings`, `GraphSettings` (Task 1).
- Produces:
  - `retrieval.base.Retriever` protocol: `retrieve(query: str, k: int) -> list[ContextItem]`; `estimate_tokens(text: str) -> int`.
  - `RagRetriever(vectors, embedder, sparse)`, `WikiRetriever(vectors, embedder, sparse)`, `GraphRetriever(graph, vectors, embedder, settings: GraphSettings)`, `CombinedRetriever(parts: dict[str, Retriever], budget: dict[str, float], max_tokens: int)`.
  - `retrieval.Mode` (`StrEnum`: `rag`, `wiki`, `graph`, `all`); `build_retrievers(vectors, graph, embedder, sparse, settings: Settings) -> dict[str, Retriever]` (keys = mode values).
  - `answer.NO_SOURCES: str`; `answer.build_context(items: list[ContextItem], max_tokens: int) -> tuple[str, list[Source], int]`; `answer.Answerer(llm: LLMClient, max_context_tokens: int)` with `async answer(question: str, mode: str, items: list[ContextItem]) -> Answer`.
  - `compare.run_mode(question, mode, retriever, answerer, k) -> Answer` (async; latency includes retrieval); `compare.compare(question, retrievers, answerer, k, runs_dir: Path, config_hash: str = "") -> list[Answer]` (async; appends to `runs_dir/compare.jsonl`); `compare.config_hash(settings: Settings) -> str`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_retrieval.py`:

```python
import json

from qdrant_client import QdrantClient

from mnogobase.models import ChunkRecord, ContextItem, EmbedInput
from mnogobase.retrieval.answer import NO_SOURCES, Answerer, build_context
from mnogobase.retrieval.combined import CombinedRetriever
from mnogobase.retrieval.compare import compare
from mnogobase.retrieval.rag import RagRetriever
from mnogobase.retrieval.wiki import WikiRetriever
from mnogobase.stores.qdrant_store import QdrantStore
from tests.fakes import FakeEmbedder, FakeLLM, FakeSparse, scripted_llm_handler


class Stub:
    def __init__(self, items):
        self.items = items

    def retrieve(self, query, k):
        return self.items[:k]


def item(kind, ref, text="x" * 40, path=None, page=None) -> ContextItem:
    return ContextItem(kind=kind, ref=ref, text=text, path=path, page=page)


def test_build_context_numbers_and_budget():
    items = [
        item("chunk", "d:00001", "alpha " * 10, path="/d/a.pdf", page=2),
        item("wiki", "p#0", "beta", path="entities/x.md"),
        item("relation", "r", "A —uses→ B"),
    ]
    context, sources, used = build_context(items, 1000)
    assert context.startswith("[1] (document a.pdf, p.2)\nalpha")
    assert "[2] (wiki entities/x.md)\nbeta" in context
    assert "[3] (graph relation)\nA —uses→ B" in context
    assert [s.n for s in sources] == [1, 2, 3] and used > 0
    _, few, _ = build_context(items, 1)
    assert len(few) == 1  # at least one source even if over budget


def test_combined_dedupes_and_respects_budget():
    shared = item("chunk", "c1")
    parts = {
        "rag": Stub([shared, item("chunk", "c2")]),
        "wiki": Stub([item("wiki", "p#0")]),
        "graph": Stub([item("entity", "e1"), shared, item("relation", "r1")]),
    }
    budget = {"rag": 0.5, "wiki": 0.25, "graph": 0.25}
    refs = [(i.kind, i.ref) for i in CombinedRetriever(parts, budget, 1000).retrieve("q", 5)]
    assert refs == [("chunk", "c1"), ("chunk", "c2"), ("wiki", "p#0"), ("entity", "e1"),
                    ("relation", "r1")]
    tight = CombinedRetriever(parts, budget, 20).retrieve("q", 5)
    assert [(i.kind, i.ref) for i in tight] == [("chunk", "c1")]


async def test_answerer_marks_cited_sources():
    llm = FakeLLM(lambda task, prompt: "Because of X [2].")
    answer = await Answerer(llm, 1000).answer("Why?", "rag", [item("chunk", "c1"), item("chunk", "c2")])
    assert answer.text == "Because of X [2]."
    assert [s.cited for s in answer.sources] == [False, True]
    assert answer.mode == "rag" and answer.tokens_in > 0 and answer.context_tokens > 0
    assert "Question: Why?" in llm.calls_for("answer")[0]


async def test_answerer_without_sources_skips_llm():
    llm = FakeLLM(scripted_llm_handler)
    answer = await Answerer(llm, 1000).answer("Why?", "graph", [])
    assert answer.text == NO_SOURCES and llm.calls == []


async def test_compare_writes_jsonl(tmp_path):
    retrievers = {m: Stub([item("chunk", f"{m}1")]) for m in ("rag", "wiki", "graph", "all")}
    answers = await compare("Q?", retrievers, Answerer(FakeLLM(scripted_llm_handler), 1000), k=3,
                            runs_dir=tmp_path, config_hash="abc")
    assert [a.mode for a in answers] == ["rag", "wiki", "graph", "all"]
    lines = (tmp_path / "compare.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    first = json.loads(lines[0])
    assert first["config_hash"] == "abc" and first["question"] == "Q?" and first["sources"]


def test_rag_and_wiki_retrievers():
    emb, sparse = FakeEmbedder(), FakeSparse()
    vectors = QdrantStore(QdrantClient(":memory:"), "t_", 64)
    vectors.ensure_collections()
    texts = ["the transformer uses attention", "cats are cute"]
    chunks = [ChunkRecord(chunk_id=f"aaaa:{i:05d}", doc_id="aaaa", idx=i, text=t, context_text=t,
                          page_start=1, path="/docs/a.pdf") for i, t in enumerate(texts)]
    vectors.upsert_chunks(chunks, emb.embed_documents([EmbedInput(text=t) for t in texts]),
                          sparse.encode_documents(texts))
    section = [("Summary", "Transformer — Summary\ntransformer attention architecture")]
    vectors.upsert_wiki_sections("p1", "e1", "entities/transformer.md", section,
                                 emb.embed_documents([EmbedInput(text=section[0][1])]),
                                 sparse.encode_documents([section[0][1]]))
    rag = RagRetriever(vectors, emb, sparse).retrieve("transformer attention", 2)
    assert rag[0].kind == "chunk" and rag[0].ref == "aaaa:00000"
    assert rag[0].path == "/docs/a.pdf" and rag[0].page == 1
    wiki = WikiRetriever(vectors, emb, sparse).retrieve("transformer", 2)
    assert wiki[0].kind == "wiki" and wiki[0].ref == "p1#0"
    assert wiki[0].path == "entities/transformer.md"
```

`tests/integration/test_graph_retriever.py`:

```python
import pytest
from qdrant_client import QdrantClient

from mnogobase.config import GraphSettings
from mnogobase.models import ChunkRecord, DocumentRecord, EmbedInput, EntityRecord
from mnogobase.retrieval.graph import GraphRetriever
from mnogobase.stores.qdrant_store import QdrantStore
from tests.fakes import FakeEmbedder

pytestmark = pytest.mark.integration


def test_graph_retriever_returns_entities_relations_and_evidence(graph):
    emb = FakeEmbedder()
    vectors = QdrantStore(QdrantClient(":memory:"), "t_", 64)
    vectors.ensure_collections()
    graph.upsert_document(DocumentRecord(doc_id="d" * 16, path="/docs/d.md", title="D",
                                         mime="text/markdown"))
    chunk = ChunkRecord(chunk_id=f"{'d' * 16}:00000", doc_id="d" * 16, idx=0,
                        text="The Transformer uses softmax.", context_text="x")
    graph.upsert_chunks([chunk])
    entities = [
        EntityRecord(entity_id="e1", name="Transformer", type="Method", description="Attention architecture."),
        EntityRecord(entity_id="e2", name="Softmax", type="Concept", description="Normalizing function."),
    ]
    for e in entities:
        graph.upsert_entity(e)
    vectors.upsert_entities(entities, emb.embed_documents(
        [EmbedInput(text=f"{e.name}: {e.description}") for e in entities]))
    graph.add_mentions(chunk.chunk_id, ["e1", "e2"])
    graph.merge_relation("e1", "e2", "uses", "Transformer uses softmax.", 5, chunk.chunk_id)

    # threshold 0.9 switches off fuzzy vector seeds, so seeds come from the fulltext index only
    retriever = GraphRetriever(graph, vectors, emb, GraphSettings(seed_threshold=0.9))
    items = retriever.retrieve("What is the Transformer?", 5)
    kinds = [i.kind for i in items]
    assert "entity" in kinds and "relation" in kinds and "chunk" in kinds
    entity = next(i for i in items if i.kind == "entity")
    assert entity.text.startswith("Transformer (Method)")
    relation = next(i for i in items if i.kind == "relation")
    assert relation.text.startswith("Transformer —uses→ Softmax")
    evidence = next(i for i in items if i.kind == "chunk")
    assert evidence.ref == chunk.chunk_id and evidence.path == "/docs/d.md"
    assert retriever.retrieve("zzz qqq", 5) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_retrieval.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mnogobase.retrieval'`.

- [ ] **Step 3: Implement the retrievers**

`src/mnogobase/retrieval/base.py`:

```python
from __future__ import annotations

from typing import Protocol

from mnogobase.models import ContextItem


class Retriever(Protocol):
    def retrieve(self, query: str, k: int) -> list[ContextItem]: ...


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)
```

`src/mnogobase/retrieval/rag.py`:

```python
from __future__ import annotations

from mnogobase.embedding.base import Embedder, SparseEncoder
from mnogobase.models import ContextItem
from mnogobase.stores.qdrant_store import QdrantStore


class RagRetriever:
    """Classic RAG: hybrid (dense + BM25, RRF) search over raw chunks."""

    def __init__(self, vectors: QdrantStore, embedder: Embedder, sparse: SparseEncoder):
        self._vectors = vectors
        self._embedder = embedder
        self._sparse = sparse

    def retrieve(self, query: str, k: int) -> list[ContextItem]:
        hits = self._vectors.search_chunks(self._embedder.embed_query(query),
                                           self._sparse.encode_query(query), k)
        return [ContextItem(kind="chunk", ref=h.key, text=h.payload["text"],
                            path=h.payload.get("path"), page=h.payload.get("page"), score=h.score)
                for h in hits]
```

`src/mnogobase/retrieval/wiki.py`:

```python
from __future__ import annotations

from mnogobase.embedding.base import Embedder, SparseEncoder
from mnogobase.models import ContextItem
from mnogobase.stores.qdrant_store import QdrantStore


class WikiRetriever:
    """Persistent-wiki mode: hybrid search over wiki page sections only."""

    def __init__(self, vectors: QdrantStore, embedder: Embedder, sparse: SparseEncoder):
        self._vectors = vectors
        self._embedder = embedder
        self._sparse = sparse

    def retrieve(self, query: str, k: int) -> list[ContextItem]:
        hits = self._vectors.search_wiki(self._embedder.embed_query(query),
                                         self._sparse.encode_query(query), k)
        return [ContextItem(kind="wiki", ref=h.key, text=h.payload["text"],
                            path=h.payload.get("path"), score=h.score) for h in hits]
```

`src/mnogobase/retrieval/graph.py`:

```python
from __future__ import annotations

from mnogobase.config import GraphSettings
from mnogobase.embedding.base import Embedder
from mnogobase.models import ContextItem
from mnogobase.stores.graph_store import GraphStore
from mnogobase.stores.qdrant_store import QdrantStore


class GraphRetriever:
    """Graph-RAG: link query to entities, expand k hops, return entities + triples + evidence."""

    def __init__(self, graph: GraphStore, vectors: QdrantStore, embedder: Embedder,
                 settings: GraphSettings):
        self._graph = graph
        self._vectors = vectors
        self._embedder = embedder
        self._s = settings

    def retrieve(self, query: str, k: int) -> list[ContextItem]:
        vector_seeds = [h.key for h in self._vectors.search_entities(
            self._embedder.embed_query(query), k=self._s.seeds, score_threshold=self._s.seed_threshold)]
        text_seeds = [eid for eid, _ in self._graph.fulltext_entities(query, self._s.seeds)]
        seeds = list(dict.fromkeys(vector_seeds + text_seeds))[: self._s.seeds * 2]
        if not seeds:
            return []
        items: list[ContextItem] = []
        for eid in seeds[: self._s.seeds]:
            entity = self._graph.get_entity(eid)
            if entity is not None:
                items.append(ContextItem(kind="entity", ref=eid,
                                         text=f"{entity.name} ({entity.type}): {entity.description}"))
        relations = self._graph.neighborhood(seeds, self._s.hops, self._s.max_relations)
        for r in relations:
            text = f"{r.src_name} —{r.predicate}→ {r.dst_name}"
            if r.description:
                text += f": {r.description}"
            items.append(ContextItem(kind="relation", ref=f"{r.src_id}|{r.predicate}|{r.dst_id}",
                                     text=text, score=r.weight))
        evidence = list(dict.fromkeys(cid for r in relations for cid in r.evidence))[:k]
        for chunk in self._graph.chunks_by_ids(evidence):
            items.append(ContextItem(kind="chunk", ref=chunk.chunk_id, text=chunk.text,
                                     path=chunk.path, page=chunk.page))
        return items
```

`src/mnogobase/retrieval/combined.py`:

```python
from __future__ import annotations

from mnogobase.models import ContextItem
from mnogobase.retrieval.base import Retriever, estimate_tokens


class CombinedRetriever:
    """`all` mode: each part gets a share of the token budget; chunks are de-duplicated."""

    def __init__(self, parts: dict[str, Retriever], budget: dict[str, float], max_tokens: int):
        self._parts = parts
        self._budget = budget
        self._max = max_tokens

    def retrieve(self, query: str, k: int) -> list[ContextItem]:
        out: list[ContextItem] = []
        seen: set[tuple[str, str]] = set()
        for name, retriever in self._parts.items():
            limit = int(self._max * self._budget.get(name, 0.0))
            used = 0
            for item in retriever.retrieve(query, k):
                key = (item.kind, item.ref)
                if key in seen:
                    continue
                cost = estimate_tokens(item.text)
                if used + cost > limit:
                    break
                seen.add(key)
                used += cost
                out.append(item)
        return out
```

`src/mnogobase/retrieval/__init__.py`:

```python
from __future__ import annotations

from enum import StrEnum

from mnogobase.config import Settings
from mnogobase.embedding.base import Embedder, SparseEncoder
from mnogobase.retrieval.base import Retriever
from mnogobase.retrieval.combined import CombinedRetriever
from mnogobase.retrieval.graph import GraphRetriever
from mnogobase.retrieval.rag import RagRetriever
from mnogobase.retrieval.wiki import WikiRetriever
from mnogobase.stores.graph_store import GraphStore
from mnogobase.stores.qdrant_store import QdrantStore


class Mode(StrEnum):
    RAG = "rag"
    WIKI = "wiki"
    GRAPH = "graph"
    ALL = "all"


def build_retrievers(vectors: QdrantStore, graph: GraphStore, embedder: Embedder,
                     sparse: SparseEncoder, settings: Settings) -> dict[str, Retriever]:
    rag = RagRetriever(vectors, embedder, sparse)
    wiki = WikiRetriever(vectors, embedder, sparse)
    graph_retriever = GraphRetriever(graph, vectors, embedder, settings.graph)
    combined = CombinedRetriever({"rag": rag, "wiki": wiki, "graph": graph_retriever},
                                 settings.retrieval.budget, settings.retrieval.context_tokens)
    return {Mode.RAG.value: rag, Mode.WIKI.value: wiki, Mode.GRAPH.value: graph_retriever,
            Mode.ALL.value: combined}
```

- [ ] **Step 4: Implement answering and comparison**

`src/mnogobase/retrieval/answer.py`:

```python
from __future__ import annotations

import re
import time
from pathlib import Path

from mnogobase.llm.client import LLMClient
from mnogobase.llm.templates import render
from mnogobase.models import Answer, ContextItem, Source
from mnogobase.retrieval.base import estimate_tokens

NO_SOURCES = "The knowledge base has no relevant sources for this question."
_CITED = re.compile(r"\[(\d+)\]")


def _label(item: ContextItem) -> str:
    if item.kind == "chunk":
        where = Path(item.path).name if item.path else item.ref
        return f"document {where}" + (f", p.{item.page}" if item.page else "")
    if item.kind == "wiki":
        return f"wiki {item.path or item.ref}"
    return f"graph {item.kind}"


def build_context(items: list[ContextItem], max_tokens: int) -> tuple[str, list[Source], int]:
    blocks: list[str] = []
    sources: list[Source] = []
    used = 0
    for item in items:
        cost = estimate_tokens(item.text)
        if sources and used + cost > max_tokens:
            break
        n = len(sources) + 1
        blocks.append(f"[{n}] ({_label(item)})\n{item.text}")
        sources.append(Source(n=n, kind=item.kind, ref=item.ref, path=item.path, page=item.page,
                              snippet=item.text[:200]))
        used += cost
    return "\n\n".join(blocks), sources, used


class Answerer:
    def __init__(self, llm: LLMClient, max_context_tokens: int):
        self._llm = llm
        self._max = max_context_tokens

    async def answer(self, question: str, mode: str, items: list[ContextItem]) -> Answer:
        start = time.perf_counter()
        context, sources, context_tokens = build_context(items, self._max)
        if not sources:
            return Answer(question=question, mode=mode, text=NO_SOURCES,
                          latency_ms=int((time.perf_counter() - start) * 1000))
        before_in, before_out = self._llm.usage.tokens_in, self._llm.usage.tokens_out
        prompt = render("answer", question=question, context=context)
        text = await self._llm.complete([{"role": "user", "content": prompt}], task="answer")
        cited = {int(n) for n in _CITED.findall(text)}
        for source in sources:
            source.cited = source.n in cited
        return Answer(
            question=question, mode=mode, text=text, sources=sources,
            latency_ms=int((time.perf_counter() - start) * 1000),
            tokens_in=self._llm.usage.tokens_in - before_in,
            tokens_out=self._llm.usage.tokens_out - before_out,
            context_tokens=context_tokens,
        )
```

`src/mnogobase/retrieval/compare.py`:

```python
from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from mnogobase.config import Settings
from mnogobase.models import Answer
from mnogobase.retrieval.answer import Answerer
from mnogobase.retrieval.base import Retriever


def config_hash(settings: Settings) -> str:
    relevant = settings.model_dump_json(include={"llm", "embedder", "chunking", "retrieval", "graph"})
    return hashlib.sha1(relevant.encode("utf-8")).hexdigest()[:12]


async def run_mode(question: str, mode: str, retriever: Retriever, answerer: Answerer, k: int) -> Answer:
    start = time.perf_counter()
    items = retriever.retrieve(question, k)
    answer = await answerer.answer(question, mode, items)
    answer.latency_ms = int((time.perf_counter() - start) * 1000)
    return answer


async def compare(question: str, retrievers: dict[str, Retriever], answerer: Answerer, k: int,
                  runs_dir: Path, config_hash: str = "") -> list[Answer]:
    # sequential on purpose: per-answer token accounting reads the shared usage counter
    answers = [await run_mode(question, mode, r, answerer, k) for mode, r in retrievers.items()]
    runs_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).isoformat()
    with (runs_dir / "compare.jsonl").open("a", encoding="utf-8") as fh:
        for answer in answers:
            row = {"ts": ts, "config_hash": config_hash, **answer.model_dump()}
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return answers
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_retrieval.py -v && uv run pytest -m integration tests/integration/test_graph_retriever.py -v`
Expected: 6 passed, then 1 passed.

- [ ] **Step 6: Commit**

```bash
git add src/mnogobase/retrieval tests/unit/test_retrieval.py tests/integration/test_graph_retriever.py
git commit -m "feat: rag/wiki/graph/all retrieval with cited answers and comparison log"
```

---

### Task 16: CLI, doctor, reindex and reset

**Files:**
- Create: `src/mnogobase/doctor.py`, `src/mnogobase/maintenance.py`, `src/mnogobase/cli.py`
- Test: `tests/unit/test_cli.py`, `tests/integration/test_maintenance.py`

**Interfaces:**
- Consumes: `app.build_app`/`App` (Task 14), `Pipeline` (Task 14), retrieval (Task 15), `Registry`/`ingest_lock`/`STAGES` (Task 4), `log.configure_logging`/`new_run_id` (Task 3), `embedder_signature` (Task 6), `split_sections` (Task 12).
- Produces:
  - `doctor.Check(name: str, ok: bool, detail: str)`; `doctor.run_checks(settings: Settings, timeout: float = 5.0) -> list[Check]`.
  - `maintenance.reindex(app: App) -> dict[str, int]` (keys `chunks`, `entities`, `wiki_sections`); `maintenance.reset(app: App) -> None`.
  - `cli.app` (Typer) with commands `doctor`, `status`, `ingest [PATHS...] [--no-wiki] [--retry-failed]`, `wiki build [--all]`, `ask QUESTION [--mode rag|wiki|graph|all] [--k N]`, `compare QUESTION [--k N]`, `reindex`, `reset [--yes]`, global `--config/-c`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_cli.py`:

```python
from types import SimpleNamespace

from typer.testing import CliRunner

from mnogobase import cli
from mnogobase.doctor import Check

runner = CliRunner()


def test_status_on_empty_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0, result.output
    assert "files: 0" in result.output


def test_doctor_exit_code_reflects_failures(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "run_checks", lambda settings: [
        Check("device", True, "mps"), Check("neo4j", False, "ServiceUnavailable")])
    result = runner.invoke(cli.app, ["doctor"])
    assert result.exit_code == 1
    assert "neo4j" in result.output and "fail" in result.output


def test_reset_requires_confirmation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    called = []
    monkeypatch.setattr(cli, "build_app", lambda settings: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(cli, "do_reset", lambda application: called.append(application))
    declined = runner.invoke(cli.app, ["reset"], input="n\n")
    assert declined.exit_code == 1 and called == []
    accepted = runner.invoke(cli.app, ["reset", "--yes"])
    assert accepted.exit_code == 0 and len(called) == 1


def test_ingest_without_paths_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    stub = SimpleNamespace(registry=SimpleNamespace(failed_paths=lambda: []), close=lambda: None)
    monkeypatch.setattr(cli, "build_app", lambda settings: stub)
    result = runner.invoke(cli.app, ["ingest", "--retry-failed"])
    assert result.exit_code == 0
    assert "Nothing to ingest" in result.output
```

`tests/integration/test_maintenance.py`:

```python
import shutil
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from mnogobase.app import build_app
from mnogobase.config import ChunkingSettings, WikiSettings, load_settings
from mnogobase.maintenance import reindex, reset
from mnogobase.pipeline import EmbedderMismatchError
from mnogobase.stores.qdrant_store import QdrantStore
from tests.fakes import FakeEmbedder, FakeLLM, FakeSparse, scripted_llm_handler

pytestmark = pytest.mark.integration
FIXTURES = Path(__file__).parents[1] / "fixtures"


async def test_reindex_after_embedder_change_then_reset(graph, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    for name in ("attention_en.md", "vnimanie_ru.md"):
        shutil.copy(FIXTURES / name, docs / name)
    settings = load_settings(tmp_path / "missing.yaml").model_copy(update={
        "data_dir": tmp_path / ".mb", "logs_dir": tmp_path / "logs",
        "wiki": WikiSettings(dir=tmp_path / "wiki", min_mentions=1),
        "chunking": ChunkingSettings(max_tokens=128),
    })
    client = QdrantClient(":memory:")

    def make(dim: int):
        return build_app(settings, embedder=FakeEmbedder(dim), sparse=FakeSparse(),
                         llm=FakeLLM(scripted_llm_handler),
                         vectors=QdrantStore(client, "t_", dim), graph=graph)

    first = make(64)
    await first.pipeline.ingest([docs])
    first.registry.close()

    second = make(32)
    second.embedder.model_id = "fake-embed-v2"
    with pytest.raises(EmbedderMismatchError):
        second.pipeline.prepare()
    stats = reindex(second)
    assert stats["chunks"] > 0 and stats["entities"] > 0 and stats["wiki_sections"] > 0
    assert second.vectors.collection_dim(second.vectors.chunks) == 32
    second.pipeline.prepare()  # signature now matches
    assert second.vectors.search_chunks(second.embedder.embed_query("attention"),
                                        second.sparse.encode_query("attention"), k=3)

    reset(second)
    assert graph.counts()["Entity"] == 0
    assert not (tmp_path / "wiki").exists()
    assert second.registry.files() == []
    assert not client.collection_exists(second.vectors.chunks)
    second.registry.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_cli.py -v`
Expected: FAIL with `ImportError: cannot import name 'cli' from 'mnogobase'`.

- [ ] **Step 3: Implement `doctor.py`**

```python
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import httpx
from qdrant_client import QdrantClient

from mnogobase.config import Settings
from mnogobase.device import detect_device
from mnogobase.registry import Registry
from mnogobase.stores.graph_store import GraphStore
from mnogobase.stores.qdrant_store import QdrantStore


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def _guard(name: str, fn: Callable[[], Check]) -> Check:
    try:
        return fn()
    except Exception as exc:  # doctor must report, never crash
        return Check(name, False, f"{type(exc).__name__}: {exc}")


def run_checks(settings: Settings, timeout: float = 5.0) -> list[Check]:
    def device() -> Check:
        return Check("device", True, detect_device(settings.device))

    def ollama() -> Check:
        resp = httpx.get(f"{settings.embedder.base_url}/api/tags", timeout=timeout)
        resp.raise_for_status()
        names = {m["name"] for m in resp.json().get("models", [])}
        wanted = settings.embedder.model
        if wanted in names or f"{wanted}:latest" in names:
            return Check("ollama", True, f"embedder {wanted} available")
        return Check("ollama", False, f"model {wanted} missing: run `ollama pull {wanted}`")

    def llm() -> Check:
        resp = httpx.get(f"{settings.llm.base_url.rstrip('/')}/models", timeout=timeout,
                         headers={"Authorization": f"Bearer {settings.llm.api_key()}"})
        resp.raise_for_status()
        ids = {m["id"] for m in resp.json().get("data", [])}
        if not ids or settings.llm.model in ids:
            return Check("llm", True, f"{settings.llm.model} @ {settings.llm.base_url}")
        return Check("llm", False, f"{settings.llm.model} is not served by {settings.llm.base_url}")

    def qdrant() -> Check:
        store = QdrantStore(QdrantClient(url=settings.qdrant.url, timeout=int(timeout)),
                            settings.qdrant.prefix, settings.embedder.dim)
        dim = store.collection_dim(store.chunks)
        if dim is not None and dim != settings.embedder.dim:
            return Check("qdrant", False, f"{store.chunks} has dim {dim}, config {settings.embedder.dim}: run `mnogobase reindex`")
        return Check("qdrant", True, f"{settings.qdrant.url} ({'no index yet' if dim is None else f'dim {dim}'})")

    def neo4j() -> Check:
        graph = GraphStore.from_settings(settings.neo4j)
        try:
            graph.verify()
        finally:
            graph.close()
        return Check("neo4j", True, settings.neo4j.uri)

    def signature() -> Check:
        db = settings.data_dir / "state.db"
        expected = f"ollama:{settings.embedder.model}:{settings.embedder.dim}"
        if not db.exists():
            return Check("index", True, "no index yet")
        registry = Registry(db)
        try:
            stored = registry.get_meta("embedder")
        finally:
            registry.close()
        if stored in (None, expected):
            return Check("index", True, f"embedder {expected}")
        return Check("index", False, f"index built with {stored}, config uses {expected}: run `mnogobase reindex`")

    checks = [("device", device), ("ollama", ollama), ("llm", llm), ("qdrant", qdrant),
              ("neo4j", neo4j), ("index", signature)]
    return [_guard(name, fn) for name, fn in checks]
```

- [ ] **Step 4: Implement `maintenance.py`**

```python
from __future__ import annotations

import shutil

from mnogobase.app import App
from mnogobase.embedding.base import embedder_signature
from mnogobase.models import EmbedInput
from mnogobase.wiki.render import split_sections


def reindex(app: App) -> dict[str, int]:
    """Recompute all vectors with the current embedder; graph and wiki files stay untouched."""
    app.vectors.drop_collections()
    app.vectors.ensure_collections()
    app.graph.ensure_schema()
    stats = {"chunks": 0, "entities": 0, "wiki_sections": 0}

    for doc_id in app.registry.doc_ids():
        if not app.pipeline.chunks_path(doc_id).exists():
            continue
        doc, chunks = app.pipeline.load_chunks(doc_id)
        if not chunks:
            continue
        texts = [c.context_text for c in chunks]
        app.vectors.upsert_chunks(
            chunks, app.embedder.embed_documents([EmbedInput(text=t, title=doc.title) for t in texts]),
            app.sparse.encode_documents(texts),
        )
        for chunk_id, entity_ids in app.graph.chunk_entity_ids(doc_id).items():
            app.vectors.set_chunk_entities(chunk_id, entity_ids)
        stats["chunks"] += len(chunks)

    entities = app.graph.entities()
    for start in range(0, len(entities), 64):
        batch = entities[start : start + 64]
        app.vectors.upsert_entities(batch, app.embedder.embed_documents(
            [EmbedInput(text=f"{e.name}: {e.description}", title=e.name) for e in batch]))
    stats["entities"] = len(entities)

    for row in app.graph.wiki_pages():
        file = app.settings.wiki.dir / row.path
        if not file.exists():
            continue
        sections = split_sections(file.read_text(encoding="utf-8"))
        if not sections:
            continue
        texts = [t for _, t in sections]
        app.vectors.upsert_wiki_sections(
            row.page_id, row.page_id, row.path, sections,
            app.embedder.embed_documents([EmbedInput(text=t, title=row.title) for t in texts]),
            app.sparse.encode_documents(texts),
        )
        stats["wiki_sections"] += len(sections)

    app.registry.set_meta("embedder", embedder_signature(app.embedder))
    return stats


def reset(app: App) -> None:
    """Delete every vector, graph node, registry row, cache file and wiki page."""
    app.vectors.drop_collections()
    app.graph.wipe()
    app.registry.wipe()
    shutil.rmtree(app.settings.data_dir / "cache", ignore_errors=True)
    shutil.rmtree(app.settings.wiki.dir, ignore_errors=True)
```

- [ ] **Step 5: Implement `cli.py`**

```python
from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path

import filelock
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from mnogobase.app import build_app
from mnogobase.config import Settings, load_settings
from mnogobase.doctor import run_checks
from mnogobase.log import configure_logging, new_run_id
from mnogobase.maintenance import reindex as do_reindex
from mnogobase.maintenance import reset as do_reset
from mnogobase.models import Answer
from mnogobase.pipeline import EmbedderMismatchError
from mnogobase.registry import STAGES, Registry, ingest_lock
from mnogobase.retrieval import Mode, build_retrievers
from mnogobase.retrieval.answer import Answerer
from mnogobase.retrieval.compare import compare as do_compare
from mnogobase.retrieval.compare import config_hash, run_mode
from mnogobase.stores.qdrant_store import DimensionMismatchError

app = typer.Typer(no_args_is_help=True, help="mnogobase — LLM Wiki core: ingest, wiki, retrieval.")
wiki_app = typer.Typer(no_args_is_help=True, help="Wiki commands.")
app.add_typer(wiki_app, name="wiki")
console = Console()
_state: dict[str, Path | None] = {"config": None}


@app.callback()
def main(config: Path | None = typer.Option(None, "--config", "-c", help="Path to config.yaml.")) -> None:
    _state["config"] = config


def _settings() -> Settings:
    settings = load_settings(_state["config"])
    configure_logging(settings.logs_dir)
    return settings


def _lock(settings: Settings) -> filelock.FileLock:
    lock = ingest_lock(settings.data_dir)
    try:
        lock.acquire()
    except filelock.Timeout:
        console.print("[red]Another ingest / wiki / reindex run is in progress.[/red]")
        raise typer.Exit(2) from None
    return lock


def _fail_on_mismatch(exc: Exception) -> None:
    console.print(f"[red]{exc}[/red]")
    raise typer.Exit(2) from exc


def _where(path: str | None, page: int | None, ref: str) -> str:
    where = Path(path).name if path else ref
    return f"{where}, p.{page}" if page else where


def _print_answer(answer: Answer) -> None:
    console.print(Panel(answer.text, title=f"{answer.mode} · {answer.latency_ms} ms"))
    table = Table("#", "cited", "kind", "source", "snippet")
    for s in answer.sources:
        table.add_row(str(s.n), "✓" if s.cited else "", s.kind, _where(s.path, s.page, s.ref),
                      s.snippet[:80])
    console.print(table)


@app.command()
def doctor() -> None:
    """Check device, Ollama, the LLM endpoint, Qdrant, Neo4j and the index signature."""
    checks = run_checks(_settings())
    table = Table("check", "status", "detail")
    for c in checks:
        table.add_row(c.name, "[green]ok[/green]" if c.ok else "[red]fail[/red]", c.detail)
    console.print(table)
    if not all(c.ok for c in checks):
        raise typer.Exit(1)


@app.command()
def status() -> None:
    """Summarize ingested files, stage states, pending wiki updates and errors."""
    settings = _settings()
    registry = Registry(settings.data_dir / "state.db")
    try:
        files = registry.files()
        counts = Counter(f.status for f in files)
        console.print(f"files: {len(files)} " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        summary = registry.stage_summary()
        table = Table("stage", "done", "failed", "pending", "running")
        for stage in STAGES:
            row = summary.get(stage, {})
            table.add_row(stage, *(str(row.get(k, 0)) for k in ("done", "failed", "pending", "running")))
        console.print(table)
        console.print(f"entities waiting for wiki update: {len(registry.dirty())}")
        errors = registry.stage_errors()
        if errors:
            err_table = Table("doc_id", "stage", "error")
            for doc_id, stage, error in errors:
                err_table.add_row(doc_id, stage, error or "")
            console.print(err_table)
    finally:
        registry.close()


@app.command()
def ingest(
    paths: list[Path] | None = typer.Argument(None, help="Files or folders to ingest."),
    no_wiki: bool = typer.Option(False, "--no-wiki", help="Skip the wiki update."),
    retry_failed: bool = typer.Option(False, "--retry-failed", help="Re-run failed stages."),
) -> None:
    """Parse, chunk, embed and extract documents into Qdrant + Neo4j, then update the wiki."""
    settings = _settings()
    lock = _lock(settings)
    try:
        application = build_app(settings)
        try:
            targets = list(paths or [])
            if not targets and retry_failed:
                targets = [Path(p) for p in application.registry.failed_paths()]
            if not targets:
                console.print("Nothing to ingest.")
                return
            run_id = new_run_id()
            with console.status("Ingesting…"):
                try:
                    report = asyncio.run(application.pipeline.ingest(
                        targets, build_wiki=not no_wiki, retry_failed=retry_failed, run_id=run_id))
                except (EmbedderMismatchError, DimensionMismatchError) as exc:
                    _fail_on_mismatch(exc)
        finally:
            application.close()
    finally:
        lock.release()
    console.print(f"run {run_id}: processed {len(report.processed)}, skipped {len(report.skipped)}, "
                  f"failed {len(report.failed)}")
    if report.wiki:
        console.print(f"wiki: created {len(report.wiki.created)}, updated {len(report.wiki.updated)}, "
                      f"deleted {len(report.wiki.deleted)}")
    for path, error in report.failed.items():
        console.print(f"[red]✗[/red] {path}: {error}")
    if report.failed:
        raise typer.Exit(1)


@wiki_app.command("build")
def wiki_build(rebuild_all: bool = typer.Option(False, "--all", help="Regenerate every page.")) -> None:
    """Regenerate wiki pages for entities with new mentions (or all with --all)."""
    settings = _settings()
    lock = _lock(settings)
    try:
        application = build_app(settings)
        try:
            try:
                application.pipeline.prepare(resume=False)
            except (EmbedderMismatchError, DimensionMismatchError) as exc:
                _fail_on_mismatch(exc)
            report = asyncio.run(application.wiki.build(rebuild_all=rebuild_all, run_id=new_run_id()))
        finally:
            application.close()
    finally:
        lock.release()
    console.print(f"wiki: created {len(report.created)}, updated {len(report.updated)}, "
                  f"skipped {len(report.skipped)}")


def _answer_setup(application):
    try:
        application.pipeline.prepare(resume=False)
    except (EmbedderMismatchError, DimensionMismatchError) as exc:
        _fail_on_mismatch(exc)
    s = application.settings
    retrievers = build_retrievers(application.vectors, application.graph, application.embedder,
                                  application.sparse, s)
    return retrievers, Answerer(application.llm, s.retrieval.context_tokens)


@app.command()
def ask(
    question: str = typer.Argument(..., help="Question to answer from the knowledge base."),
    mode: Mode = typer.Option(Mode.ALL, "--mode", "-m", help="rag | wiki | graph | all"),
    k: int | None = typer.Option(None, "--k", help="Results per retriever."),
) -> None:
    """Answer a question with citations using one retrieval mode."""
    application = build_app(_settings())
    try:
        retrievers, answerer = _answer_setup(application)
        answer = asyncio.run(run_mode(question, mode.value, retrievers[mode.value], answerer,
                                      k or application.settings.retrieval.k))
    finally:
        application.close()
    _print_answer(answer)


@app.command()
def compare(
    question: str = typer.Argument(..., help="Question to answer in all four modes."),
    k: int | None = typer.Option(None, "--k", help="Results per retriever."),
) -> None:
    """Answer in rag / wiki / graph / all and log the comparison to runs/compare.jsonl."""
    application = build_app(_settings())
    try:
        retrievers, answerer = _answer_setup(application)
        s = application.settings
        answers = asyncio.run(do_compare(question, retrievers, answerer, k or s.retrieval.k,
                                         s.runs_dir, config_hash(s)))
    finally:
        application.close()
    table = Table("mode", "answer", "sources (cited)", "latency ms", "tokens in/out", "context")
    for a in answers:
        cited = ", ".join(_where(x.path, x.page, x.ref) for x in a.sources if x.cited) or "-"
        table.add_row(a.mode, a.text, cited, str(a.latency_ms), f"{a.tokens_in}/{a.tokens_out}",
                      str(a.context_tokens))
    console.print(table)


@app.command()
def reindex() -> None:
    """Recompute every vector after changing the embedder (graph and wiki are kept)."""
    settings = _settings()
    lock = _lock(settings)
    try:
        application = build_app(settings)
        try:
            with console.status("Re-embedding…"):
                stats = do_reindex(application)
        finally:
            application.close()
    finally:
        lock.release()
    console.print(f"reindexed: {stats}")


@app.command()
def reset(yes: bool = typer.Option(False, "--yes", help="Do not ask for confirmation.")) -> None:
    """Delete ALL vectors, graph data, registry state, caches and wiki pages."""
    if not yes and not typer.confirm("Delete all mnogobase data (Qdrant, Neo4j, wiki, cache)?"):
        raise typer.Exit(1)
    settings = _settings()
    lock = _lock(settings)
    try:
        application = build_app(settings)
        try:
            do_reset(application)
        finally:
            application.close()
    finally:
        lock.release()
    console.print("All data deleted.")
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_cli.py -v && uv run pytest -m integration tests/integration/test_maintenance.py -v`
Expected: 4 passed, then 1 passed.

- [ ] **Step 7: Check the CLI help renders**

Run: `uv run mnogobase --help && uv run mnogobase wiki --help && uv run mnogobase ask --help`
Expected: help text listing `doctor, status, ingest, wiki, ask, compare, reindex, reset`; `ask` shows `--mode` with choices `rag|wiki|graph|all`.

- [ ] **Step 8: Commit**

```bash
git add src/mnogobase/doctor.py src/mnogobase/maintenance.py src/mnogobase/cli.py tests/unit/test_cli.py tests/integration/test_maintenance.py
git commit -m "feat: CLI with doctor, status, ingest, wiki, ask, compare, reindex, reset"
```

---

### Task 17: End-to-end test on real services, README

**Files:**
- Create: `tests/e2e/test_e2e.py`, `README.md`

**Interfaces:**
- Consumes: everything above; real Ollama (`embeddinggemma-2:740m`), the LLM endpoint from `.env`, Docker for testcontainers (Neo4j + Qdrant).
- Produces: a runnable `-m e2e` test and the team-facing README.

- [ ] **Step 1: Write the e2e test**

`tests/e2e/test_e2e.py`:

```python
import shutil
from pathlib import Path

import pytest

from mnogobase.app import build_app
from mnogobase.config import load_settings
from mnogobase.ids import normalize_name
from mnogobase.retrieval import build_retrievers
from mnogobase.retrieval.answer import Answerer
from mnogobase.retrieval.compare import run_mode
from mnogobase.stores.graph_store import GraphStore
from mnogobase.stores.qdrant_store import QdrantStore

pytestmark = pytest.mark.e2e
ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture(scope="module")
def services():
    from testcontainers.neo4j import Neo4jContainer
    from testcontainers.qdrant import QdrantContainer

    with Neo4jContainer("neo4j:5.26-community", password="testpassword") as neo, \
            QdrantContainer("qdrant/qdrant:v1.19.2") as qdrant:
        yield neo, qdrant


def make_corpus(folder: Path) -> None:
    from docx import Document
    from reportlab.pdfgen import canvas

    folder.mkdir()
    for name in ("attention_en.md", "vnimanie_ru.md"):
        shutil.copy(FIXTURES / name, folder / name)
    docx = Document()
    docx.add_heading("Transformer architecture notes", 0)
    docx.add_paragraph("The Transformer replaced recurrent networks in machine translation. "
                       "Its attention mechanism uses softmax to weight the values.")
    docx.save(folder / "notes.docx")
    pdf = canvas.Canvas(str(folder / "rag.pdf"))
    lines = ["Retrieval-Augmented Generation", "",
             "Retrieval-augmented generation (RAG) combines a retriever with a language model.",
             "Documents are embedded with a Transformer encoder and stored in a vector database.",
             "At question time the most similar passages are added to the prompt."]
    text = pdf.beginText(72, 750)
    for line in lines:
        text.textLine(line)
    pdf.drawText(text)
    pdf.save()


async def test_full_flow(services, tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)  # .env with LLM_API_KEY lives in the repo root
    neo, qdrant = services
    corpus = tmp_path / "corpus"
    make_corpus(corpus)
    base = load_settings(ROOT / "config.yaml")
    settings = base.model_copy(update={
        "data_dir": tmp_path / ".mb", "logs_dir": tmp_path / "logs", "runs_dir": tmp_path / "runs",
        "wiki": base.wiki.model_copy(update={"dir": tmp_path / "wiki"}),
    })
    graph = GraphStore(neo.get_driver())
    vectors = QdrantStore(qdrant.get_client(), "e2e_", settings.embedder.dim)
    app = build_app(settings, vectors=vectors, graph=graph)
    try:
        report = await app.pipeline.ingest([corpus])
        assert report.failed == {}, report.failed
        assert len(report.processed) == 4

        transformers = [e for e in graph.entities() if normalize_name(e.name) == "transformer"]
        assert transformers, [e.name for e in graph.entities()]
        assert max(e.mention_count for e in transformers) >= 2

        pages = list((tmp_path / "wiki" / "entities").glob("*.md"))
        assert pages
        assert any("[^" in p.read_text(encoding="utf-8") for p in pages)

        retrievers = build_retrievers(vectors, graph, app.embedder, app.sparse, settings)
        answerer = Answerer(app.llm, settings.retrieval.context_tokens)
        for mode in ("rag", "wiki", "graph", "all"):
            answer = await run_mode("What is the Transformer and who proposed it?", mode,
                                    retrievers[mode], answerer, settings.retrieval.k)
            assert answer.sources, mode
            assert answer.text.strip(), mode

        again = await app.pipeline.ingest([corpus])
        assert again.processed == [] and len(again.skipped) == 4
    finally:
        app.close()
```

- [ ] **Step 2: Run the e2e test**

Run: `ollama list | grep embeddinggemma-2:740m && uv run pytest -m e2e -v -s`
Expected: 1 passed. If entity naming from the real LLM makes the `transformer` assertion fail, print `[e.name for e in graph.entities()]` from the failure, inspect the extraction prompt output in `logs/`, and fix the prompt (bump `PROMPT_VERSION` in `extractor.py` so cached results are not reused) — do not loosen the assertion to pass.

- [ ] **Step 3: Write `README.md`**

````markdown
# mnogobase

Core of the **LLM Wiki** project: turns a folder of documents (PDF, DOCX, PPTX, XLSX, HTML, Markdown, …) into

- a **vector index** in Qdrant (EmbeddingGemma 2 dense vectors + BM25 sparse vectors),
- a **knowledge graph** in Neo4j (entities, relations, provenance for every fact),
- a **persistent wiki** of Markdown pages (Obsidian-compatible) that is updated incrementally,

and answers questions with citations in four retrieval modes: `rag`, `wiki`, `graph`, `all`.

```
files ─► Docling parse ─► HybridChunker ─► EmbeddingGemma 2 + BM25 ─► Qdrant
                                     └──► LLM extraction ─► entity resolution ─► Neo4j
                                                                    └──► wiki/*.md (+ Qdrant wiki_pages)
question ─► rag | wiki | graph | all ─► cited answer (+ runs/compare.jsonl)
```

## Quick start

```bash
cp .env.example .env               # put LLM_API_KEY=... into .env
docker compose up -d               # Qdrant :6333, Neo4j :7474/:7687
ollama pull embeddinggemma-2:740m
uv sync
uv run mnogobase doctor
uv run mnogobase ingest ./docs
uv run mnogobase ask "What is attention?" --mode graph
uv run mnogobase compare "What is attention?"
```

Neo4j Browser: http://localhost:7474 (user `neo4j`, password from `.env`). Wiki: open `wiki/` in Obsidian.

## Commands

| Command | What it does |
|---|---|
| `doctor` | checks device, Ollama model, LLM endpoint, Qdrant, Neo4j, index signature |
| `status` | files and stage states, failed stages with errors, entities waiting for wiki update |
| `ingest PATH... [--no-wiki] [--retry-failed]` | incremental ingest; unchanged files are skipped, changed files replace their old version |
| `wiki build [--all]` | regenerate pages of entities with new mentions (or all) |
| `ask "Q" --mode rag\|wiki\|graph\|all` | cited answer from one mode |
| `compare "Q"` | all four modes side by side, appended to `runs/compare.jsonl` |
| `reindex` | re-embed everything after changing the embedder (graph and wiki are kept) |
| `reset [--yes]` | delete all data |

## Configuration

`config.yaml` holds all non-secret settings; secrets go to `.env`. Any value can be overridden with env vars, e.g. `MNOGOBASE_LLM__MODEL=gemma4:26b-a4b`, `MNOGOBASE_EMBEDDER__DIM=512`.

- **LLM** — any OpenAI-compatible endpoint. Local: `llm.base_url: http://localhost:11434/v1`, `llm.model: gemma4:26b-a4b`. Per-task models: `llm.overrides.{extract,resolve,wiki,answer}`.
- **Embedder** — Ollama `embeddinggemma-2:740m`, 768d (Matryoshka: 512/256 also work). Fallback `qwen3-embedding:0.6b` (dim 1024, see comments in `config.yaml`). After changing model or dim run `mnogobase reindex`.
- **Wiki** — `wiki.language` (default `en`), `wiki.min_mentions` (entities with fewer mentions get no page).

## GPU / CUDA

`device: auto` picks CUDA → MPS → CPU for Docling layout/OCR models. On Linux with NVIDIA:

```bash
docker compose -f docker-compose.yml -f docker-compose.cuda.yml up -d   # GPU Qdrant indexing + Ollama in Docker
uv pip uninstall onnxruntime && uv pip install onnxruntime-gpu          # GPU BM25 in fastembed
```

For a faster LLM on CUDA, serve a model with vLLM and point `llm.base_url` at it.

## Tests

```bash
uv run pytest                    # unit tests
uv run pytest -m integration     # needs Docker (testcontainers: Neo4j)
uv run pytest -m e2e -s          # needs Docker + Ollama + LLM endpoint
```

## Layout

`src/mnogobase/`: `parsing/` (Docling), `chunking/`, `embedding/`, `llm/` (client + prompts), `extraction/` (extractor, entity resolver), `stores/` (Qdrant, Neo4j), `wiki/`, `retrieval/`, `pipeline.py`, `cli.py`. Design: `docs/superpowers/specs/2026-10-07-mnogobase-ingest-design.md`.

## Next steps (out of scope here)

Desktop UI, FastAPI/MCP server, Deep Research agent, knowledge-gap and contradiction detection, image/audio embedding (Docling already saves pictures to `.mnogobase/cache/<doc_id>/images/`), Grafana/Loki dashboards, Langfuse tracing, automatic RAG vs Wiki vs Graph evaluation on `runs/compare.jsonl`.
````

- [ ] **Step 4: Full verification**

Run: `uv run ruff check . && uv run ruff format --check . && uv run pytest && uv run pytest -m integration`
Expected: ruff clean; all unit and integration tests pass.

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/test_e2e.py README.md
git commit -m "test: end-to-end flow on real services; docs: README"
```
