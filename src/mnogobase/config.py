from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, YamlConfigSettingsSource

LLM_TASKS = ("extract", "resolve", "wiki", "answer")
LANGUAGE_NAMES = {"en": "English", "ru": "Russian"}
# Entity types: name -> one-line description for the extraction prompt. The type is part
# of entity_id, so the types must not overlap; `Other` stays last (the fallback).
DEFAULT_ENTITY_TYPES: dict[str, str] = {
    "Person": "A real or fictional individual, named or clearly identified.",
    "Organization": (
        "A company, institution, university, government body, team or community; "
        "not the place it is located in (Location)."
    ),
    "Location": "A country, city, region, geographic feature or a specific facility or building.",
    "Event": (
        "A dated happening such as a conference, incident, battle, release or election; "
        "not an ongoing initiative (Project)."
    ),
    "Project": (
        "A named initiative, programme or mission with a goal; not a one-off happening (Event)."
    ),
    "Product": (
        "A physical or commercial good or service that is not software, or a brand; "
        "software goes to Software."
    ),
    "Software": (
        "A program, app, library, framework, programming language or software platform; "
        "a trained ML model goes to AIModel."
    ),
    "Technology": (
        "A general technology, standard, protocol or hardware category, not a specific "
        "product or program (e.g. 5G, lithium-ion battery, blockchain)."
    ),
    "AIModel": (
        "A named trained machine-learning model or model family (e.g. GPT-4, BERT); "
        "the software running it goes to Software, the architecture or technique to Method."
    ),
    "Method": (
        "A technique, algorithm, procedure or process for doing something (e.g. gradient "
        "descent, PCR); not an abstract idea (Concept) or a technology (Technology)."
    ),
    "Concept": (
        "An abstract idea, theory, principle or term; not a way of doing something (Method) "
        "or a whole discipline (Field)."
    ),
    "Field": (
        "A discipline or domain of knowledge or industry (e.g. machine learning, oncology, "
        "banking); not a single idea within it (Concept)."
    ),
    "Work": (
        "A book, paper, article, report, film, artwork, song or other created work, "
        "named by its title."
    ),
    "Dataset": "A named data collection, corpus or benchmark.",
    "Metric": (
        "A measure or indicator (e.g. accuracy, BLEU, GDP, inflation rate); "
        "not a benchmark (Dataset)."
    ),
    "Regulation": "A law, regulation, policy, treaty or other legal act.",
    "Substance": "A chemical, material, drug or food ingredient.",
    "Condition": "A disease, disorder, symptom or other medical or psychological condition.",
    "Organism": "A species, organism, cell type or other living thing.",
    "Other": "Anything meaningful that fits none of the types above.",
}
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
    # name -> description (YAML mapping, order kept); a plain list of names also works
    entity_types: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_ENTITY_TYPES))
    max_failed_ratio: float = 0.2

    @field_validator("entity_types", mode="before")
    @classmethod
    def _names_or_mapping(cls, value: object) -> object:
        if isinstance(value, list | tuple):
            value = dict(_list_entry(item) for item in value)
        if not isinstance(value, dict):
            return value  # pydantic reports the wrong type
        # `Name:` without a description is null in YAML
        types = {str(k).strip(): str(v or "").strip() for k, v in value.items()}
        if not types:
            raise ValueError("entity_types must list at least one type")
        seen: dict[str, str] = {}
        for name in [str(k) for k in value]:
            if not name.strip():
                raise ValueError("entity_types: a type name is blank")
            if ":" in name or "\n" in name or "\r" in name:
                problem = "':'" if ":" in name else "a newline"
                raise ValueError(f"entity_types: type name {name!r} must not contain {problem}")
            key = name.strip().casefold()
            if key in seen:
                raise ValueError(
                    f"entity_types: duplicate type name {name.strip()!r} (same as {seen[key]!r}; "
                    "names are compared case-insensitively)"
                )
            seen[key] = name.strip()
        return types


def _list_entry(item: object) -> tuple[str, object]:
    """One entry of a YAML list of types: `- Name` or `- Name: description`."""
    if isinstance(item, dict):
        if len(item) != 1:
            raise ValueError(
                f"entity_types: a list entry must be a name or a mapping with one key, got {item!r}"
            )
        return next(iter(item.items()))
    return str(item), ""


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


class _YamlSource(YamlConfigSettingsSource):
    """config.yaml, except that an entity type set given by init or env replaces the YAML
    one as a whole: sources are deep-merged, which would add the YAML types to it."""

    def __call__(self) -> dict[str, Any]:
        data = super().__call__()
        higher = self.current_state.get("extract")
        own = data.get("extract")
        if isinstance(higher, dict) and "entity_types" in higher and isinstance(own, dict):
            data = {**data, "extract": {k: v for k, v in own.items() if k != "entity_types"}}
        return data


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
            _YamlSource(settings_cls),
            file_secret_settings,
        )


def load_settings(config_path: Path | None = None) -> Settings:
    """Load `.env` secrets from the CWD, then settings from YAML + env."""
    load_dotenv(Path.cwd() / ".env", override=False)
    explicit = config_path or os.environ.get("MNOGOBASE_CONFIG")
    path = Path(explicit or "config.yaml")
    # A missing implicit ./config.yaml means "use defaults"; a missing explicit path is a typo.
    if explicit and not path.is_file():
        raise FileNotFoundError(f"Config file not found: {path}")

    class _FileSettings(Settings):
        model_config = {**Settings.model_config, "yaml_file": path}

    return _FileSettings()
