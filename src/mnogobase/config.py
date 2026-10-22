from __future__ import annotations

import os
from pathlib import Path
from typing import Any, ClassVar, Literal, Self

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, YamlConfigSettingsSource

LLM_TASKS = ("extract", "resolve", "wiki", "answer", "query")
LANGUAGE_NAMES = {"en": "English", "ru": "Russian"}
# Типы сущностей: имя -> однострочное описание для промпта извлечения. Тип входит
# в entity_id, поэтому типы не должны пересекаться; `Other` идёт последним (запасной).
DEFAULT_ENTITY_TYPES: dict[str, str] = {
    "Person": "A real or fictional individual, named or clearly identified.",
    "Organization": (
        "A company, institution, university, government body, team or community; "
        "not the place it is located in (Location)."
    ),
    "Location": (
        "A place on Earth or in space, such as a country, city, region, geographic feature, "
        "facility, building or an astronomical object (planet, star, galaxy)."
    ),
    "Event": (
        "A dated happening such as a conference, incident, battle, release or election, "
        "or a named historical period; not an ongoing initiative (Project)."
    ),
    "Project": (
        "A named initiative, programme or mission with a goal; not a one-off happening (Event)."
    ),
    "Product": (
        "A commercial good or service, or a brand; software goes to Software, an instrument "
        "or machine used for work or research to Equipment."
    ),
    "Equipment": (
        "A specific instrument, apparatus, machine, spacecraft or other device used for work "
        "or research (e.g. VLT/SPHERE, James Webb Space Telescope, Voyager 1); a general "
        "hardware category goes to Technology."
    ),
    "Software": (
        "A program, app, library, framework, programming language, software platform "
        "or trained machine-learning model (e.g. PyTorch, GPT-4)."
    ),
    "Technology": (
        "A general technology, standard, protocol or hardware category, not a specific "
        "product or program (e.g. 5G, lithium-ion battery, blockchain)."
    ),
    "Method": (
        "A technique, algorithm or procedure that someone applies (e.g. gradient descent, PCR); "
        "a natural process or phenomenon goes to Concept, a technology to Technology."
    ),
    "Concept": (
        "An abstract idea, theory, principle, term, natural process or phenomenon, or a whole "
        "discipline (e.g. inflation, photosynthesis, machine learning)."
    ),
    "Work": (
        "A book, paper, article, report, journal, film, artwork, song, dataset, benchmark "
        "or other created work, named by its title."
    ),
    "Metric": (
        "A measure, indicator or unit (e.g. accuracy, BLEU, GDP, inflation rate, "
        "astronomical unit); a benchmark goes to Work."
    ),
    "Regulation": "A law, regulation, policy, treaty or other legal act.",
    "Substance": (
        "A chemical, material, drug, food ingredient or biomolecule such as a protein or hormone."
    ),
    "Condition": "A disease, disorder, symptom or other medical or psychological condition.",
    "Organism": "A species, organism, cell type, organ or other living thing or part of one.",
    "Other": "Anything meaningful that fits none of the types above.",
}
DEFAULT_EXTENSIONS = [
    "pdf",
    "docx",
    "pptx",
    "xlsx",
    "html",
    "htm",
    "md",
    "adoc",
    "csv",
    "txt",
    # картинки и скриншоты: OCR и (при vision.enabled) описание VLM
    "png",
    "jpg",
    "jpeg",
    "tif",
    "tiff",
    "bmp",
    "webp",
]


Serve = Literal["llamacpp", "vllm", "api"]


class ServerSettings(BaseModel):
    """Как `mnogobase models up` запускает модель роли (при `serve: llamacpp | vllm`)."""

    ctx: int = Field(default=8192, ge=512)  # окно контекста: --ctx-size / --max-model-len
    gpu_memory: float = Field(default=0.15, gt=0, lt=1)  # vLLM: доля ВСЕЙ видеопамяти
    args: list[str] = Field(default_factory=list)  # дополнительные флаги сервера


class ServedModel(BaseModel):
    """Модель на OpenAI-совместимом сервере.

    `serve` — где она работает: `llamacpp` или `vllm` поднимает `mnogobase models up` на порту
    роли (`port`), `api` — уже работающий сервер или API по `base_url`. `model` — HF-
    репозиторий исходной модели и её имя на сервере на любом бэкенде (у llama.cpp это
    `--alias`); веса для llama.cpp — GGUF из `gguf` (`repo:квант`)."""

    role: ClassVar[str]
    port: ClassVar[int]

    model: str
    serve: Serve = "llamacpp"
    gguf: str | None = None
    base_url: str | None = None  # до /v1 включительно; по умолчанию localhost:<port> роли
    api_key_env: str | None = None  # переменная с Bearer-ключом сервера (--api-key)
    server: ServerSettings = Field(default_factory=ServerSettings)

    @model_validator(mode="after")
    def _check_serve(self) -> Self:
        if self.serve == "llamacpp" and not self.gguf:
            raise ValueError(
                f"{self.role}.gguf is required for serve: llamacpp "
                "(a GGUF repo with quant, e.g. Qwen/Qwen3-Embedding-4B-GGUF:Q4_K_M)"
            )
        if self.serve == "api" and not self.base_url:
            raise ValueError(f"{self.role}.base_url is required for serve: api")
        return self

    @property
    def url(self) -> str:
        """Адрес OpenAI-API сервера, до /v1 включительно."""
        return (self.base_url or f"http://localhost:{self.port}/v1").rstrip("/")

    @property
    def weights(self) -> str:
        """Что именно работает на сервере: GGUF у llama.cpp, иначе `model`."""
        return self.gguf if self.serve == "llamacpp" and self.gguf else self.model


class LLMSettings(ServedModel):
    role: ClassVar[str] = "llm"
    port: ClassVar[int] = 8000

    model: str = "gpt-6-luna"
    serve: Serve = "api"
    base_url: str | None = "https://codex.sale/v1"
    api_key_env: str | None = "LLM_API_KEY"
    server: ServerSettings = Field(
        default_factory=lambda: ServerSettings(ctx=32768, gpu_memory=0.6)
    )
    concurrency: int = 4
    temperature: float = 0.0
    timeout_s: float = 120.0
    overrides: dict[str, str | None] = Field(default_factory=lambda: dict.fromkeys(LLM_TASKS))

    def model_for(self, task: str) -> str:
        return self.overrides.get(task) or self.model

    def api_key(self) -> str:
        # локальные серверы игнорируют ключ, но OpenAI SDK не принимает пустой
        return (os.environ.get(self.api_key_env) if self.api_key_env else None) or "local"


class EmbedderSettings(ServedModel):
    """`/embeddings`; по умолчанию Qwen3-Embedding-4B на llama.cpp."""

    role: ClassVar[str] = "embedder"
    port: ClassVar[int] = 8001

    model: str = "Qwen/Qwen3-Embedding-4B"
    gguf: str | None = "Qwen/Qwen3-Embedding-4B-GGUF:Q4_K_M"
    dim: int = 2560
    batch_size: int = 32
    doc_template: str = "{text}"
    query_template: str = (
        "Instruct: Given a question, retrieve passages that answer it\nQuery: {query}"
    )

    @property
    def model_id(self) -> str:
        """Какие веса считают векторы — часть подписи индекса: смена бэкенда или кванта
        требует `reindex`."""
        return f"{self.serve}:{self.weights}"


class SparseSettings(BaseModel):
    model: str = "Qdrant/bm25"


class ParsingSettings(BaseModel):
    ocr: bool = True
    # языки OCR (BCP-47 с префиксом iso:); без них Docling берёт китайскую модель RapidOCR
    ocr_lang: list[str] = Field(default_factory=lambda: ["iso:ru", "iso:en"])
    extensions: list[str] = Field(default_factory=lambda: list(DEFAULT_EXTENSIONS))


class ChunkingSettings(BaseModel):
    # `auto` — токенизатор модели эмбеддера с её сервера; иначе это HF-репозиторий
    tokenizer: str = "auto"
    max_tokens: int = 512


class ExtractSettings(BaseModel):
    # имя -> описание (YAML mapping, порядок сохраняется); подойдёт и простой список имён
    entity_types: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_ENTITY_TYPES))
    max_failed_ratio: float = 0.2

    @field_validator("entity_types", mode="before")
    @classmethod
    def _names_or_mapping(cls, value: object) -> object:
        if isinstance(value, list | tuple):
            value = dict(_list_entry(item) for item in value)
        if not isinstance(value, dict):
            return value  # о неверном типе сообщит pydantic
        # `Name:` без описания в YAML — это null
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
    """Один элемент YAML-списка типов: `- Name` или `- Name: description`."""
    if isinstance(item, dict):
        if len(item) != 1:
            raise ValueError(
                f"entity_types: a list entry must be a name or a mapping with one key, got {item!r}"
            )
        return next(iter(item.items()))
    return str(item), ""


class ResolveSettings(BaseModel):
    # 1.0: по одному косинусу не сливать, всё через LLM. Замер на размеченных парах: при 0.92
    # вектор склеивал "X. Zhang" с "H. Zhang" и "Gaia DR3" с "DR2", а верные пары давали 0.75-0.97
    auto_merge: float = 1.0
    llm_check: float = 0.75
    max_descriptions: int = 5


class WikiSettings(BaseModel):
    dir: Path = Path("wiki")
    language: str = "en"
    min_mentions: int = 2
    evidence_k: int = 12
    obsidian: bool = True  # писать .obsidian/graph.json: фильтр и цвета по типам


class RetrievalSettings(BaseModel):
    k: int = 8
    context_tokens: int = 6000
    neighbors: int = Field(default=0, ge=0)  # rag: доп. чанки той же секции с каждой стороны хита
    translate: bool = True  # искать ещё и по английскому переводу неанглийского вопроса
    answer_language: str = "auto"  # код языка (en, ru, ...) или auto: язык вопроса
    budget: dict[str, float] = Field(
        default_factory=lambda: {"rag": 0.4, "wiki": 0.3, "graph": 0.3}
    )


class RerankSettings(ServedModel):
    """Cross-encoder reranking хитов rag: `/rerank` с Qwen3-Reranker (llama.cpp — GGUF с
    классификатором yes/no, например от gorse-io; vLLM — исходная модель)."""

    role: ClassVar[str] = "rerank"
    port: ClassVar[int] = 8002

    enabled: bool = False
    model: str = "Qwen/Qwen3-Reranker-4B"
    gguf: str | None = "gorse-io/Qwen3-Reranker-4B-GGUF:Q4_K_M"
    candidates: int = Field(default=30, ge=1)  # гибридных хитов на скоринг; остаются лучшие k
    max_chars: int = Field(default=4000, ge=100)  # обрезка текста документа для реранкера


DEFAULT_VISION_PROMPT = (
    "Describe this image. If it is a chart, diagram or table, "
    "say what it shows, name its axes or parts and give the key values and trends. "
    "Transcribe the visible text verbatim in its original language. Be factual and concise."
)


class VisionSettings(ServedModel):
    """Описание картинок при разборе: Docling отправляет каждую картинку документа на
    `/chat/completions` VLM, описание попадает в текст документа рядом с картинкой."""

    role: ClassVar[str] = "vision"
    port: ClassVar[int] = 8003

    enabled: bool = False
    model: str = "Qwen/Qwen3-VL-2B-Instruct"
    gguf: str | None = "unsloth/Qwen3-VL-2B-Instruct-GGUF:Q4_K_M"
    prompt: str = DEFAULT_VISION_PROMPT
    max_tokens: int = Field(default=400, ge=16)
    # маленькие VLM на temperature 0 зацикливаются; presence_penalty — рекомендация Qwen3-VL
    params: dict[str, Any] = Field(
        default_factory=lambda: {"temperature": 0.0, "presence_penalty": 1.5}
    )
    min_area: float = Field(default=0.05, ge=0, le=1)  # доля площади страницы
    # классы картинок (классификатор Docling), которые не описываются
    skip: list[str] = Field(
        default_factory=lambda: ["logo", "icon", "qr_code", "bar_code", "signature", "stamp"]
    )
    concurrency: int = Field(default=4, ge=1)
    timeout_s: float = 120.0

    @field_validator("skip")
    @classmethod
    def _known_labels(cls, value: list[str]) -> list[str]:
        from docling_core.types.doc.labels import PictureClassificationLabel

        known = {label.value for label in PictureClassificationLabel}
        unknown = [v for v in value if v not in known]
        if unknown:
            raise ValueError(
                f"vision.skip: unknown picture class {', '.join(unknown)} "
                f"(known: {', '.join(sorted(known))})"
            )
        return value


class GraphSettings(BaseModel):
    hops: int = 2
    max_relations: int = 30
    seeds: int = 5
    seed_threshold: float = 0.3  # мин. косинус для seed-сущностей по запросу


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
    """config.yaml, но набор типов сущностей из init или env заменяет YAML-набор целиком:
    источники сливаются deep merge, и иначе к нему добавились бы типы из YAML."""

    def __call__(self) -> dict[str, Any]:
        data = super().__call__()
        higher = self.current_state.get("extract")
        own = data.get("extract")
        if isinstance(higher, dict) and "entity_types" in higher and isinstance(own, dict):
            data = {
                **data,
                "extract": {k: v for k, v in own.items() if k != "entity_types"},
            }
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
    rerank: RerankSettings = Field(default_factory=RerankSettings)
    vision: VisionSettings = Field(default_factory=VisionSettings)
    graph: GraphSettings = Field(default_factory=GraphSettings)
    qdrant: QdrantSettings = Field(default_factory=QdrantSettings)
    neo4j: Neo4jSettings = Field(default_factory=Neo4jSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # приоритет: явный init > env MNOGOBASE_* > config.yaml > значения по умолчанию
        return (
            init_settings,
            env_settings,
            _YamlSource(settings_cls),
            file_secret_settings,
        )


_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def _bypass_proxy_for_local_hosts() -> None:
    """Не пускает запросы к локальным сервисам (llama.cpp, Qdrant) через системный прокси.

    На Windows httpx при отсутствии env-переменных *_PROXY берёт прокси из реестра, но
    игнорирует список исключений из реестра, поэтому запросы к localhost идут в прокси (503).
    """
    current = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    hosts = [h.strip() for h in current.split(",") if h.strip()]
    if "*" in hosts:
        return
    hosts += [h for h in _LOCAL_HOSTS if h not in hosts]
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = ",".join(hosts)


def load_settings(config_path: Path | None = None) -> Settings:
    """Загружает секреты из `.env` в CWD, затем настройки из YAML + env."""
    load_dotenv(Path.cwd() / ".env", override=False)
    _bypass_proxy_for_local_hosts()
    explicit = config_path or os.environ.get("MNOGOBASE_CONFIG")
    path = Path(explicit or "config.yaml")
    # Отсутствие неявного ./config.yaml значит "берём умолчания"; отсутствие явно
    # указанного пути — опечатка.
    if explicit and not path.is_file():
        raise FileNotFoundError(f"Config file not found: {path}")

    class _FileSettings(Settings):
        model_config = {**Settings.model_config, "yaml_file": path}

    return _FileSettings()
