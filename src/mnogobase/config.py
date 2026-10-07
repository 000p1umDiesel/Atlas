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
    "Person",
    "Organization",
    "Concept",
    "Method",
    "Technology",
    "Dataset",
    "Work",
    "Event",
    "Location",
    "Other",
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
        return (
            init_settings,
            env_settings,
            YamlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )


def load_settings(config_path: Path | None = None) -> Settings:
    """Load `.env` secrets from the CWD, then settings from YAML + env."""
    load_dotenv(Path.cwd() / ".env", override=False)
    path = Path(config_path or os.environ.get("MNOGOBASE_CONFIG", "config.yaml"))

    class _FileSettings(Settings):
        model_config = {**Settings.model_config, "yaml_file": path}

    return _FileSettings()
