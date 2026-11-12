"""Настройки mnogobase: pydantic-модели всех секций `config.yaml` и их загрузка.

`load_settings` вызывается первым делом в CLI, API и worker'е; полученный `Settings`
передаётся дальше в `build_app` и во все компоненты, каждый берёт свою секцию
(`settings.embedder`, `settings.graph` и т. д.). Источники значений по убыванию
приоритета: явные аргументы конструктора, переменные окружения `MNOGOBASE_<SECTION>__<KEY>`
(списки и словари — в JSON), `config.yaml` из текущего каталога, значения по умолчанию.
`.env` хранит только секреты: настройки ссылаются на них по имени переменной
(`api_key_env`, `password_env`), а сами значения читаются из окружения в момент
использования.

Конфиг строгий: каждая секция наследует `Section` с `extra="forbid"`, поэтому опечатка в
ключе или значение вне допустимого диапазона — ошибка валидации при старте, а не тихо
проигнорированная настройка. Модели, работающие на OpenAI-совместимых серверах (LLM,
embedder, reranker, VLM), наследуют `ServedModel`.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar, Literal, Self

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, YamlConfigSettingsSource

# Задачи LLM: по ним `llm.overrides` выбирает модель для конкретного вида вызовов.
LLM_TASKS = ("extract", "resolve", "wiki", "answer", "query")
# Код языка из настроек -> имя языка, которое подставляется в промпты.
LANGUAGE_NAMES = {"en": "English", "ru": "Russian"}
# Типы сущностей: имя -> однострочное описание для промпта извлечения. Тип входит
# в entity_id, поэтому типы не должны пересекаться: одна и та же сущность под двумя типами
# стала бы двумя узлами графа. `Other` идёт последним — запасной тип для всего остального.
# Описания на английском, потому что попадают в промпт как есть.
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
# Расширения файлов (без точки, в нижнем регистре), которые ingest и watcher берут в работу.
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


# Где работает модель роли: `llamacpp` — llama-server на порту роли, `api` — внешний сервер.
Serve = Literal["llamacpp", "api"]


class Section(BaseModel):
    """Секция конфига: неизвестный ключ (опечатка в YAML или env) — ошибка, а не тихий игнор."""

    model_config = ConfigDict(extra="forbid")


class ServerSettings(Section):
    """Как `mnogobase models up` запускает llama-server роли (при `serve: llamacpp`).

    В Docker эти же параметры должны совпадать с командой сервиса модели в `compose.yaml`:
    там сервер запускает compose, а не `models up`."""

    ctx: int = Field(default=8192, ge=512)  # окно контекста в токенах: --ctx-size
    args: list[str] = Field(default_factory=list)  # дополнительные флаги llama-server как есть


class ServedModel(Section):
    """Модель на OpenAI-совместимом сервере — общая база секций llm, embedder, rerank, vision.

    `serve` — где она работает: `llamacpp` — llama-server на порту роли (`port`; локально его
    поднимает `mnogobase models up`, в compose — сервис модели), `api` — уже работающий
    сервер или API по `base_url`. `model` — что запускается и как модель называется на
    сервере: у llama.cpp — GGUF-репозиторий с квантом (`repo:квант`, токенизатор берётся из
    самого GGUF), у `api` — имя модели на сервере. `gguf` (только llama.cpp, необязательно) —
    веса отдельно от имени: тогда запускается он, а `model` остаётся именем (`--alias`).
    `timeout_s` — предел одного HTTP-запроса к серверу.

    Подклассы задают `role` (имя секции, используется в сообщениях и `models up`) и `port`
    (порт по умолчанию: 8000 llm, 8001 embedder, 8002 rerank, 8003 vision)."""

    role: ClassVar[str]
    port: ClassVar[int]

    model: str
    serve: Serve = "llamacpp"
    gguf: str | None = None
    base_url: str | None = None  # до /v1 включительно; по умолчанию localhost:<port> роли
    api_key_env: str | None = None  # переменная с Bearer-ключом сервера (--api-key)
    server: ServerSettings = Field(default_factory=ServerSettings)
    timeout_s: float = Field(default=120.0, gt=0)

    @model_validator(mode="after")
    def _check_serve(self) -> Self:
        """Для `serve: api` адрес обязателен: угадывать, где работает чужой сервер, нельзя."""
        if self.serve == "api" and not self.base_url:
            raise ValueError(f"{self.role}.base_url обязателен при serve: api")
        return self

    @property
    def url(self) -> str:
        """Адрес OpenAI-API сервера, до /v1 включительно, без завершающего `/`."""
        return (self.base_url or f"http://localhost:{self.port}/v1").rstrip("/")

    @property
    def weights(self) -> str:
        """Что именно работает на сервере: `gguf`, если он задан у llama.cpp, иначе `model`."""
        return self.gguf if self.serve == "llamacpp" and self.gguf else self.model


class LLMSettings(ServedModel):
    """Генеративная LLM (`/chat/completions`): извлечение сущностей, проверка слияний,
    страницы wiki, ответы и перевод вопросов.

    Через неё проходят тексты документов, поэтому по умолчанию это локальный сервер;
    внешний API включается только явно. `overrides` позволяет отдать отдельную задачу
    из `LLM_TASKS` другой модели того же сервера."""

    role: ClassVar[str] = "llm"
    port: ClassVar[int] = 8000

    # По умолчанию — локальный OpenAI-совместимый сервер на порту роли. Внешний API
    # (куда уходят тексты документов) задаётся только явно в config.yaml.
    model: str = "local"
    serve: Serve = "api"
    base_url: str | None = "http://localhost:8000/v1"
    api_key_env: str | None = "LLM_API_KEY"
    server: ServerSettings = Field(default_factory=lambda: ServerSettings(ctx=32768))
    concurrency: int = Field(default=4, ge=1)  # одновременных запросов к серверу (семафор)
    temperature: float = Field(default=0.0, ge=0, le=2)
    timeout_s: float = Field(default=120.0, gt=0)
    retry_deadline_s: float = Field(default=900.0, gt=0)  # общий предел повторов одного вызова
    # задача -> имя модели; null — общая `model`
    overrides: dict[str, str | None] = Field(default_factory=lambda: dict.fromkeys(LLM_TASKS))

    def model_for(self, task: str) -> str:
        """Имя модели для задачи `task`: переопределение из `overrides` или общая `model`."""
        return self.overrides.get(task) or self.model

    def api_key(self) -> str:
        """Ключ API из переменной `api_key_env`; без неё или при пустом значении — `local`."""
        # локальные серверы игнорируют ключ, но OpenAI SDK не принимает пустой
        return (os.environ.get(self.api_key_env) if self.api_key_env else None) or "local"


class EmbedderSettings(ServedModel):
    """Плотные векторы (`/embeddings`) для чанков, страниц wiki и сущностей; по умолчанию
    Qwen3-Embedding-4B на llama.cpp.

    Шаблоны превращают текст в вход модели: `doc_template` — для индексируемых текстов,
    `query_template` — для вопросов (у Qwen3-Embedding запрос должен нести инструкцию).
    Модель (`model_id`), размерность и хэш шаблонов образуют подпись индекса в registry:
    смена любой из них блокирует ingest и ask до `mnogobase reindex`."""

    role: ClassVar[str] = "embedder"
    port: ClassVar[int] = 8001

    model: str = "Qwen/Qwen3-Embedding-4B-GGUF:Q4_K_M"
    # `auto` — полная размерность модели (узнаётся пробным запросом); число меньше неё —
    # Matryoshka-усечение
    dim: PositiveInt | Literal["auto"] = "auto"
    batch_size: int = Field(default=32, ge=1)  # текстов в одном запросе к серверу
    doc_template: str = "{text}"  # плейсхолдеры: {text} (обязателен) и {title}
    query_template: str = (
        "Instruct: Given a question, retrieve passages that answer it\nQuery: {query}"
    )

    @model_validator(mode="after")
    def _check_templates(self) -> Self:
        """Проверяет шаблоны пробной подстановкой: допустимы только известные плейсхолдеры,
        обязательные (`{text}`, `{query}`) должны присутствовать."""
        # Ошибка в шаблоне иначе всплывает только посреди ingest или ask.
        try:
            self.doc_template.format(title="t", text="x")
            self.query_template.format(query="q")
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(
                "в шаблонах эмбеддера допустимы только {text}/{title} (doc) и {query} "
                "(query); буквальные фигурные скобки пишите как {{ }}"
            ) from exc
        if "{text}" not in self.doc_template or "{query}" not in self.query_template:
            raise ValueError(
                "embedder.doc_template должен содержать {text}, а query_template — {query}"
            )
        return self

    @property
    def model_id(self) -> str:
        """Какие веса считают векторы — часть подписи индекса: смена бэкенда или кванта
        требует `reindex`."""
        return f"{self.serve}:{self.weights}"


class SparseSettings(Section):
    """Разреженные векторы для гибридного поиска в Qdrant: модель fastembed (BM25),
    считается локально, без сервера."""

    model: str = "Qdrant/bm25"


class ParsingSettings(Section):
    """Разбор документов Docling'ом: OCR, допустимые расширения и пределы размера,
    после которых стадия parse отказывается от файла."""

    ocr: bool = True
    # языки OCR (BCP-47 с префиксом iso:); без них Docling берёт китайскую модель RapidOCR.
    # RapidOCR использует только первый язык: iso:ru выбирает его кириллическую модель
    ocr_lang: list[str] = Field(default_factory=lambda: ["iso:ru", "iso:en"])
    extensions: list[str] = Field(default_factory=lambda: list(DEFAULT_EXTENSIONS))
    # больше — стадия parse падает сразу, а не держит worker часами (Docling на CPU)
    max_pages: int = Field(default=1000, ge=1)
    max_file_mb: int = Field(default=200, ge=1)


class ChunkingSettings(Section):
    """Нарезка документа на чанки; размер считается токенизатором модели эмбеддера, чтобы
    чанк гарантированно помещался в её вход."""

    # `auto` — токенизатор модели эмбеддера с её сервера (llama.cpp `/tokenize`);
    # иначе это HF-репозиторий токенизатора
    tokenizer: str = "auto"
    max_tokens: int = Field(default=512, ge=16)  # предел размера одного чанка в токенах


class ExtractSettings(Section):
    """LLM-извлечение сущностей и связей из чанков.

    Набор типов входит в версию промпта (ключ кэша извлечения), поэтому его смена
    сбрасывает кэш; уже обработанные документы сохраняют старые типы до `reset` + `ingest`."""

    # имя -> описание (YAML mapping, порядок сохраняется); подойдёт и простой список имён
    entity_types: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_ENTITY_TYPES))
    # доля чанков с неудачным извлечением, выше которой стадия extract документа падает
    max_failed_ratio: float = 0.2

    @field_validator("entity_types", mode="before")
    @classmethod
    def _names_or_mapping(cls, value: object) -> object:
        """Приводит `entity_types` к словарю имя -> описание и проверяет имена.

        Принимает YAML mapping или список (`- Name` / `- Name: description`). Имена
        обрезаются по краям; пустые, с `:` или переводом строки (они ломали бы строку
        типа в промпте) и повторы без учёта регистра — `ValueError`."""
        if isinstance(value, list | tuple):
            value = dict(_list_entry(item) for item in value)
        if not isinstance(value, dict):
            return value  # о неверном типе сообщит pydantic
        # `Name:` без описания в YAML — это null
        types = {str(k).strip(): str(v or "").strip() for k, v in value.items()}
        if not types:
            raise ValueError("entity_types: укажите хотя бы один тип")
        seen: dict[str, str] = {}
        for name in [str(k) for k in value]:
            if not name.strip():
                raise ValueError("entity_types: пустое имя типа")
            if ":" in name or "\n" in name or "\r" in name:
                problem = "':'" if ":" in name else "перевод строки"
                raise ValueError(f"entity_types: имя типа {name!r} не должно содержать {problem}")
            key = name.strip().casefold()
            if key in seen:
                raise ValueError(
                    f"entity_types: имя типа {name.strip()!r} повторяется (совпадает с "
                    f"{seen[key]!r}: регистр не учитывается)"
                )
            seen[key] = name.strip()
        return types


def _list_entry(item: object) -> tuple[str, object]:
    """Один элемент YAML-списка типов: `- Name` или `- Name: description`.

    Возвращает пару (имя, описание); словарь с несколькими ключами — `ValueError`."""
    if isinstance(item, dict):
        if len(item) != 1:
            raise ValueError(
                f"entity_types: элемент списка должен быть именем или словарём с одним ключом, а не {item!r}"
            )
        return next(iter(item.items()))
    return str(item), ""


class ResolveSettings(Section):
    """Слияние новой сущности с уже существующей (entity resolution).

    Кандидаты с косинусом не ниже `llm_check` проверяет LLM (`resolve_same.md`), с косинусом
    не ниже `auto_merge` — сливаются без проверки. Больше `max_descriptions` описаний у
    сущности сводятся LLM в одно (`resolve_summarize.md`)."""

    # 1.0: по одному косинусу не сливать, всё через LLM. Замер на размеченных парах: при 0.92
    # вектор склеивал "X. Zhang" с "H. Zhang" и "Gaia DR3" с "DR2", а верные пары давали 0.75-0.97
    auto_merge: float = 1.0
    llm_check: float = 0.75  # ниже — кандидат по вектору даже не рассматривается
    max_descriptions: int = 5


class WikiSettings(Section):
    """Markdown-wiki: страницы сущностей, которые LLM пишет по фрагментам документов."""

    dir: Path = Path("wiki")  # относительно текущего каталога
    language: str = "en"  # код языка страниц (en, ru, ...)
    min_mentions: int = 2  # страница появляется у сущности с не меньшим числом упоминаний
    evidence_k: int = 12  # сколько чанков-доказательств получает LLM на одну страницу
    obsidian: bool = True  # писать .obsidian/graph.json: фильтр и цвета по типам


class RetrievalSettings(Section):
    """Поиск контекста для ответа: размер выдачи, бюджет токенов контекста и его деление
    между режимами в `all` (`CombinedRetriever`)."""

    k: int = Field(default=8, ge=1)  # хитов на режим по умолчанию (`ask --k` переопределяет)
    context_tokens: int = Field(default=6000, ge=1)  # бюджет контекста в промпте ответа
    neighbors: int = Field(default=0, ge=0)  # rag: доп. чанки той же секции с каждой стороны хита
    translate: bool = True  # искать ещё и по английскому переводу неанглийского вопроса
    answer_language: str = "auto"  # код языка (en, ru, ...) или auto: язык вопроса
    # доли бюджета режимов в `all`; сумма не больше 1, остаток не используется
    budget: dict[str, float] = Field(
        default_factory=lambda: {"rag": 0.4, "wiki": 0.3, "graph": 0.3}
    )

    @field_validator("budget")
    @classmethod
    def _check_budget(cls, value: dict[str, float]) -> dict[str, float]:
        """Ключи только rag/wiki/graph, доли неотрицательны, сумма не больше 1."""
        unknown = sorted(set(value) - {"rag", "wiki", "graph"})
        if unknown:
            raise ValueError(f"retrieval.budget: неизвестный режим {', '.join(unknown)}")
        if any(share < 0 for share in value.values()):
            raise ValueError("retrieval.budget: доли должны быть >= 0")
        # допуск на погрешность float: 0.4 + 0.3 + 0.3 не всегда ровно 1.0
        if sum(value.values()) > 1 + 1e-9:
            raise ValueError("retrieval.budget: сумма долей должна быть не больше 1")
        return value


class RerankSettings(ServedModel):
    """Cross-encoder reranking хитов rag: `/rerank` с Qwen3-Reranker (llama.cpp — GGUF с
    классификатором yes/no, например от gorse-io; старые конвертации грузятся, но врут).

    Гибридный поиск берёт `candidates` хитов, реранкер их пересортировывает, остаются
    лучшие `retrieval.k`."""

    role: ClassVar[str] = "rerank"
    port: ClassVar[int] = 8002

    enabled: bool = False
    model: str = "gorse-io/Qwen3-Reranker-4B-GGUF:Q4_K_M"
    candidates: int = Field(default=30, ge=1)  # гибридных хитов на скоринг; остаются лучшие k
    max_chars: int = Field(default=4000, ge=100)  # обрезка текста документа для реранкера


# Промпт VLM по умолчанию (`vision.prompt`): описать картинку (для графиков и схем — оси,
# значения, тренды) и дословно переписать видимый текст на языке оригинала.
DEFAULT_VISION_PROMPT = (
    "Describe this image. If it is a chart, diagram or table, "
    "say what it shows, name its axes or parts and give the key values and trends. "
    "Transcribe the visible text verbatim in its original language. Be factual and concise."
)


class VisionSettings(ServedModel):
    """Описание картинок при разборе: Docling отправляет каждую картинку документа на
    `/chat/completions` VLM, описание попадает в текст документа рядом с картинкой.

    Отдельные файлы-картинки описываются целиком. Описание становится частью текста,
    поэтому попадает в чанки, граф и цитаты. Мелкие картинки (`min_area`) и классы из
    `skip` не описываются."""

    role: ClassVar[str] = "vision"
    port: ClassVar[int] = 8003

    enabled: bool = False
    model: str = "prithivMLmods/Xiaomi-OCR-0-GGUF:Q6_K"
    prompt: str = DEFAULT_VISION_PROMPT
    max_tokens: int = Field(default=400, ge=16)  # предел длины одного описания
    # маленькие VLM на temperature 0 зацикливаются; presence_penalty — рекомендация Qwen3-VL
    params: dict[str, Any] = Field(
        default_factory=lambda: {"temperature": 0.0, "presence_penalty": 1.5}
    )
    min_area: float = Field(default=0.05, ge=0, le=1)  # доля площади страницы
    # классы картинок (классификатор Docling), которые не описываются
    skip: list[str] = Field(
        default_factory=lambda: ["logo", "icon", "qr_code", "bar_code", "signature", "stamp"]
    )
    concurrency: int = Field(default=4, ge=1)  # одновременных запросов к VLM
    timeout_s: float = Field(default=120.0, gt=0)

    @field_validator("skip")
    @classmethod
    def _known_labels(cls, value: list[str]) -> list[str]:
        """Каждый класс из `skip` должен быть меткой классификатора картинок Docling:
        опечатка иначе молча ничего бы не отфильтровала."""
        # импорт внутри: docling_core тяжёлый, а конфиг грузят все команды
        from docling_core.types.doc.labels import PictureClassificationLabel

        known = {label.value for label in PictureClassificationLabel}
        unknown = [v for v in value if v not in known]
        if unknown:
            raise ValueError(
                f"vision.skip: неизвестный класс картинок {', '.join(unknown)} "
                f"(известные: {', '.join(sorted(known))})"
            )
        return value


class GraphAnalysisSettings(Section):
    """Пределы офлайн-анализа графа (`mnogobase graph analyze`, GDS) и APOC-обхода при
    поиске. Анализ отказывается работать на графе больше `max_nodes` / `max_relationships`
    или если оценка памяти GDS превышает `memory_mb`."""

    max_nodes: PositiveInt = 100_000
    max_relationships: PositiveInt = 500_000
    memory_mb: PositiveInt = 1536  # бюджет памяти проекции и алгоритмов GDS
    timeout_s: float = Field(default=120.0, gt=0, allow_inf_nan=False)  # на каждый вызов GDS
    apoc_max_nodes: PositiveInt = 20_000  # предел узлов APOC BFS (graph.traversal: apoc)
    apoc_timeout_s: float = Field(default=5.0, gt=0, allow_inf_nan=False)


class GraphCommunitySettings(Section):
    """Поиск по сообществам: режим graph добавляет чанки-источники сущностей из Leiden-
    сообществ seed-сущностей. Работает только при `graph.use_gds` и сохранённом снимке
    `graph analyze`."""

    enabled: bool = False
    max_communities: int = Field(default=3, gt=0, le=20)
    max_entities: int = Field(default=30, gt=0, le=500)  # сущностей из сообществ на запрос
    max_chunks: int = Field(default=3, gt=0, le=100)  # чанков из сообществ в контексте
    timeout_s: float = Field(default=5.0, gt=0, allow_inf_nan=False)


class GraphSettings(Section):
    """Режим поиска graph: seed-сущности по вопросу, обход их окрестности в Neo4j и
    (опционально) сохранённые GDS-оценки PageRank и сообщества."""

    hops: int = 2  # глубина обхода от seed-сущностей
    max_relations: int = 30  # предел связей в контексте
    seeds: int = 5  # seed-сущностей на запрос
    seed_threshold: float = 0.3  # мин. косинус для seed-сущностей по запросу
    traversal: Literal["cypher", "apoc"] = "cypher"  # apoc — ограниченный BFS по APOC
    use_gds: bool = False  # читать снимок `graph analyze` (сам GDS при ask не запускается)
    community: GraphCommunitySettings = Field(default_factory=GraphCommunitySettings)
    analysis: GraphAnalysisSettings = Field(default_factory=GraphAnalysisSettings)


class QdrantSettings(Section):
    """Подключение к Qdrant; все коллекции проекта получают префикс `prefix`."""

    url: str = "http://localhost:6333"
    prefix: str = "mb_"
    api_key_env: str | None = None  # переменная с API-ключом Qdrant (в compose обязателен)

    def api_key(self) -> str | None:
        """Ключ из переменной `api_key_env`; пустое значение или её отсутствие — `None`."""
        return os.environ.get(self.api_key_env) or None if self.api_key_env else None


class Neo4jSettings(Section):
    """Подключение к Neo4j; пароль — в переменной `password_env` (обычно из `.env`)."""

    uri: str = "bolt://localhost:7687"
    user: str = "neo4j"
    password_env: str = "NEO4J_PASSWORD"

    def password(self) -> str:
        """Пароль из окружения; пустая строка, если переменная не задана."""
        return os.environ.get(self.password_env, "")


class _YamlSource(YamlConfigSettingsSource):
    """config.yaml, но набор типов сущностей из init или env заменяет YAML-набор целиком:
    источники сливаются deep merge, и иначе к нему добавились бы типы из YAML.

    Неизвестная секция верхнего уровня в YAML — ошибка: у `Settings` стоит
    `extra="ignore"` (ради посторонних переменных `MNOGOBASE_*`), поэтому строгость для
    файла проверяется здесь."""

    def __call__(self) -> dict[str, Any]:
        data = super().__call__()
        unknown = sorted(set(data) - set(self.settings_cls.model_fields))
        if unknown:
            raise ValueError(f"config.yaml: неизвестный ключ {', '.join(unknown)}")
        # `current_state` — уже собранные значения источников с большим приоритетом
        higher = self.current_state.get("extract")
        own = data.get("extract")
        if isinstance(higher, dict) and "entity_types" in higher and isinstance(own, dict):
            data = {
                **data,
                "extract": {k: v for k, v in own.items() if k != "entity_types"},
            }
        return data


class EvaluationSettings(Section):
    """Ragas-судья для `mnogobase eval`. Незаданные поля берутся из `llm` (модель задачи
    answer) и `embedder`, так что по умолчанию судит та же модель, что отвечает."""

    base_url: str | None = None
    model: str | None = None
    api_key_env: str | None = None
    embedding_url: str | None = None
    embedding_model: str | None = None
    embedding_api_key_env: str | None = None
    timeout_s: float = Field(default=360, gt=0)
    temperature: float = Field(default=0, ge=0, le=2)
    strictness: PositiveInt = 3  # передаётся метрикам Ragas как `strictness`


class DatabaseSettings(Section):
    """PostgreSQL: адрес — здесь, пароль — в `.env` (`password_env`). Полный DSN в
    переменной `url_env` (если задана) переопределяет оба.

    Всё состояние проекта (registry, очередь, загрузки, пользователи, учёт LLM) живёт в
    одной схеме `<schema_prefix>_project_<project>`; шаблоны имён гарантируют, что имя
    схемы безопасно подставлять в SQL и укладывается в 63 символа PostgreSQL."""

    host: str = "localhost"
    port: int = Field(default=5432, ge=1, le=65535)
    name: str = "mnogobase"
    user: str = "mnogobase"
    password_env: str = "POSTGRES_PASSWORD"
    url_env: str = "DATABASE_URL"
    schema_prefix: str = Field(default="mnogobase", pattern=r"^[a-z][a-z0-9_]{0,15}$")
    project: str = Field(default="default", pattern=r"^[a-z][a-z0-9_]{0,36}$")
    connect_timeout: int = Field(default=5, ge=1, le=60)  # секунды на установку соединения


class JobsSettings(Section):
    """Фоновая очередь (`worker run`): повторы упавших задач и предел времени одной задачи."""

    max_attempts: int = Field(default=5, ge=1)
    retry_delay_s: float = Field(default=30.0, gt=0)  # удваивается с каждой попыткой, до 1 ч
    timeout_s: float = Field(default=3600.0, gt=0)  # зависший разбор не держит worker вечно


class LLMRate(Section):
    """Цена модели для оценки стоимости в учёте LLM (`llm_usage`)."""

    currency: str = Field(pattern=r"^[A-Z]{3}$")  # код ISO 4217, например USD
    input_per_million: Decimal = Field(ge=0, allow_inf_nan=False)
    output_per_million: Decimal = Field(ge=0, allow_inf_nan=False)


class ServiceSettings(Section):
    """HTTP API (`mnogobase api serve`) и его клиент."""

    # оценка стоимости внешнего LLM: имя модели -> цены за миллион токенов
    llm_rates: dict[str, LLMRate] = Field(default_factory=dict)
    api_url: str = "http://127.0.0.1:8080"  # адрес по умолчанию для `mnogobase client`
    uploads_dir: Path = Path("documents/uploads")  # документы, загруженные через API
    session_hours: int = Field(default=24 * 7, ge=1, le=720)  # срок жизни токена сессии
    max_upload_mb: int = Field(default=50, ge=1)
    max_pending_jobs: int = Field(default=100, ge=1)  # pending + running; при пределе — 429
    history_messages: int = Field(default=12, ge=0, le=100)  # сообщений беседы в промпте
    max_concurrent_asks: int = Field(default=4, ge=1, le=64)  # одновременных ask/search
    # токены LLM (вход + выход, все вызовы, включая ingest) за сутки UTC; после него API
    # отвечает 429 на вопросы, поиск и загрузки. null — без лимита
    daily_token_limit: int | None = Field(default=None, ge=1)


class Settings(BaseSettings):
    """Корень конфигурации: глобальные пути и устройство плюс все секции.

    Создаётся через `load_settings`; пути относительны текущего каталога, поэтому команды
    запускаются из корня проекта. `extra="ignore"` терпит посторонние переменные
    `MNOGOBASE_*`, а неизвестные ключи `config.yaml` ловит `_YamlSource`."""

    model_config = SettingsConfigDict(
        env_prefix="MNOGOBASE_", env_nested_delimiter="__", extra="ignore"
    )

    device: Literal["auto", "mps", "cpu"] = "auto"  # устройство для Docling (OCR, layout)
    data_dir: Path = Path(".mnogobase")  # кэши чанков и Docling, processing.jsonl, маркер
    logs_dir: Path = Path("logs")
    runs_dir: Path = Path("runs")  # результаты compare и eval
    service: ServiceSettings = Field(default_factory=ServiceSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    jobs: JobsSettings = Field(default_factory=JobsSettings)
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
    evaluation: EvaluationSettings = Field(default_factory=EvaluationSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        """Порядок источников pydantic-settings: первый в кортеже важнее.

        `dotenv_settings` сознательно не используется: `.env` содержит секреты, а не
        настройки, и загружается в окружение отдельно (`load_settings`)."""
        # приоритет: явный init > env MNOGOBASE_* > config.yaml > значения по умолчанию
        return (
            init_settings,
            env_settings,
            _YamlSource(settings_cls),
            file_secret_settings,
        )


def load_settings(config_path: Path | None = None) -> Settings:
    """Загружает секреты из `.env` в CWD, затем настройки из YAML + env.

    Путь к конфигу: `config_path` (опция `--config`), иначе переменная `MNOGOBASE_CONFIG`,
    иначе `./config.yaml`. Уже заданные переменные окружения `.env` не перезаписывает.

    Raises:
        FileNotFoundError: явно указанный файл конфига не существует.
        pydantic.ValidationError / ValueError: конфиг не прошёл проверку.
    """
    load_dotenv(Path.cwd() / ".env", override=False)
    explicit = config_path or os.environ.get("MNOGOBASE_CONFIG")
    path = Path(explicit or "config.yaml")
    # Отсутствие неявного ./config.yaml значит "берём умолчания"; отсутствие явно
    # указанного пути — опечатка.
    if explicit and not path.is_file():
        raise FileNotFoundError(f"Файл конфига не найден: {path}")

    # Путь к YAML задаётся в model_config класса, поэтому нужен подкласс на каждый вызов.
    class _FileSettings(Settings):
        model_config = {**Settings.model_config, "yaml_file": path}

    return _FileSettings()
