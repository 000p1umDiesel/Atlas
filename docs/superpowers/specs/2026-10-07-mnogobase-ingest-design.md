# mnogobase — ядро LLM Wiki: ingest → Vector + Knowledge Graph → Wiki → Retrieval

**Дата:** 2026-10-07
**Статус:** design approved, ожидает ревью спеки
**Контекст:** проект 159 «LLM Wiki: desktop-сервис для персональной базы знаний» (трек DL, MIDDLE/PRO). Этот документ описывает первый подпроект — переиспользуемое ядро, поверх которого команда строит desktop-приложение, чат, Deep Research, MCP и т.д.

---

## 1. Цель и критерии успеха

**Цель.** Python-пакет + CLI, который:

1. принимает текстовые документы (pdf, docx, pptx, xlsx, html, md, adoc, csv, txt) на русском и английском;
2. парсит и чанкирует их через Docling;
3. кладёт чанки в Qdrant (dense EmbeddingGemma 2 + sparse BM25);
4. извлекает LLM-ом сущности и связи в Knowledge Graph (Neo4j);
5. генерирует и инкрементально обновляет persistent wiki (markdown, EN) в духе LLM Wiki Карпаты;
6. отвечает на вопросы с цитированием в 4 режимах retrieval (`rag`, `wiki`, `graph`, `all`) и умеет сравнивать их между собой.

**Критерии успеха:**

- `mnogobase ingest ./docs` на смешанном RU/EN корпусе проходит без ручного вмешательства; повторный запуск без изменений файлов — no-op; изменённый файл переиндексируется без дублей.
- После ingest в Neo4j есть сущности и связи с `evidence` на чанки, в `wiki/entities/` есть страницы с валидными цитатами и `[[ссылками]]`, есть `index.md` и `log.md`.
- `mnogobase ask "<q>" --mode {rag,wiki,graph,all}` возвращает ответ со ссылками на источники (файл, страница, chunk_id).
- `mnogobase compare "<q>"` печатает 4 ответа рядом и пишет строку в `runs/compare.jsonl`.
- Падение процесса посреди ingest → повторный запуск продолжает с упавшего этапа без дублей.
- Смена LLM-провайдера (Ollama ↔ любой OpenAI-compatible) — только правкой конфига.
- Unit + integration тесты зелёные; e2e проходит на локальной Ollama.

## 2. Решения и обоснования

| Вопрос | Решение | Почему |
|---|---|---|
| Векторная БД | **Qdrant** (Docker) | named dense + sparse vectors, встроенный hybrid (RRF), payload-фильтры; есть GPU-образ |
| Keyword search | **Sparse BM25 в Qdrant** (`fastembed`, `Qdrant/bm25`) | не нужен отдельный Elasticsearch; fusion на стороне БД |
| Графовая БД | **Neo4j 5 Community** + APOC + GDS (Docker) | Cypher, Neo4j Browser для визуального исследования графа, GDS (Leiden/Louvain, centrality) пригодится для knowledge gaps и community summaries |
| Извлечение графа | **Свой пайплайн**, не LightRAG/Graphiti | полный контроль над Docling-чанками, wiki и retrieval; три архитектуры сравниваются на одних данных. LightRAG — кандидат во внешний baseline |
| Парсинг/чанкинг | **Docling** `DocumentConverter` + `HybridChunker` | структурно-осознанный, токен-осознанный чанкинг, много форматов, OCR |
| Эмбеддер | **EmbeddingGemma 2** через Ollama (`embeddinggemma-2:740m`, 768d) | мультиязычная, MRL (768/512/256), единое пространство для текста/изображений/аудио на будущее |
| LLM | **Один OpenAI-compatible клиент**; по умолчанию внешний endpoint `https://codex.sale/v1`, модель `gpt-6-luna` (ключ в `.env`); локальная альтернатива — Ollama `/v1` + `gemma4:26b-a4b` | Ollama, vLLM, LM Studio, OpenRouter, OpenAI — одним кодом. Structured output — строгая JSON Schema (`strict: true`, `additionalProperties: false`, все поля required): её требуют OpenAI-подобные провайдеры, Ollama её тоже принимает |
| Состояние пайплайна | **SQLite** (`.mnogobase/state.db`) | файлы, этапы, ошибки, dirty-сущности; восстановление и инкрементальность |
| Интерфейс | **Python-пакет + CLI (Typer)** | FastAPI/MCP/desktop навешиваются позже поверх того же пакета |
| Язык wiki | **EN** (настраивается) | канонические имена сущностей на EN склеивают RU/EN упоминания |
| Логи | **structlog**: rich-консоль + JSONL-файл | Grafana/Loki и Langfuse — вне скоупа, формат логов к ним готов |

## 3. Архитектура

```
mnogobase/
├── docker-compose.yml        # qdrant, neo4j(+APOC,GDS)
├── docker-compose.cuda.yml   # override для NVIDIA: qdrant gpu-nvidia + ollama c GPU (Linux)
├── config.yaml               # несекретные настройки
├── .env                      # секреты: NEO4J_PASSWORD, LLM_API_KEY
├── wiki/                     # сгенерированная wiki (Obsidian-совместимая)
├── logs/mnogobase.jsonl
├── runs/compare.jsonl
├── .mnogobase/               # state.db, cache/<doc_id>.json, cache/<doc_id>/images/
├── src/mnogobase/
│   ├── config.py             # pydantic-settings: YAML + env
│   ├── device.py             # auto-detect cuda > mps > cpu
│   ├── logging.py            # structlog setup, run_id context
│   ├── models.py             # доменные dataclass/pydantic: Document, Chunk, Entity, Relation, WikiPage, EmbedInput
│   ├── ids.py                # детерминированные ID
│   ├── parsing/docling_parser.py
│   ├── chunking/hybrid.py
│   ├── embedding/
│   │   ├── base.py           # Embedder protocol
│   │   ├── ollama.py         # dense
│   │   └── sparse.py         # BM25 (fastembed)
│   ├── llm/
│   │   ├── client.py         # OpenAI-compatible, structured output, retries
│   │   └── prompts/          # extract.md, resolve.md, wiki_page.md, answer.md (версионируются)
│   ├── extraction/
│   │   ├── extractor.py      # chunk → entities + relations
│   │   └── resolver.py       # entity resolution / merge
│   ├── stores/
│   │   ├── qdrant_store.py   # VectorStore
│   │   ├── graph_store.py    # GraphStore (Neo4j)
│   │   └── registry.py       # SQLite state
│   ├── wiki/
│   │   ├── builder.py        # генерация/обновление страниц
│   │   ├── render.py         # markdown, frontmatter, ссылки, index.md, log.md
│   │   └── validate.py       # проверка цитат и ссылок
│   ├── retrieval/
│   │   ├── rag.py  wiki.py  graph.py  combined.py
│   │   ├── answer.py         # LLM-ответ с [n]-цитатами
│   │   └── compare.py
│   ├── pipeline.py           # оркестрация этапов ingest
│   └── cli.py
└── tests/ (unit/, integration/, e2e/, fixtures/)
```

**Принципы:**

- Каждый модуль скрыт за узким интерфейсом (`Embedder`, `LLMClient`, `VectorStore`, `GraphStore`, `Registry`) — реализации заменяемы (другой эмбеддер, FalkorDB вместо Neo4j), модули тестируются с фейками.
- Все этапы **идемпотентны**: детерминированные ID + upsert. Повтор этапа даёт тот же результат.
- Ollama работает нативно на хосте (Metal на macOS; Docker на Mac не даёт GPU).

### 3.1 Интерфейсы (ключевые)

```python
class EmbedInput:            # text сейчас; image/audio — задел
    modality: Literal["text", "image", "audio"]
    text: str | None
    path: Path | None
    title: str | None

class Embedder(Protocol):
    model_id: str
    dim: int
    def embed_documents(self, items: list[EmbedInput]) -> list[list[float]]: ...
    def embed_query(self, query: str) -> list[float]: ...

class LLMClient(Protocol):
    def complete(self, messages, *, task: str) -> str: ...
    def structured(self, messages, schema: type[BaseModel], *, task: str) -> BaseModel: ...
```

`task` (`extract | resolve | wiki | answer`) выбирает модель через `llm.overrides` и тегирует логи.

## 4. Модель данных

### 4.1 Идентификаторы

| Объект | ID |
|---|---|
| Document | `doc_id = sha256(file_bytes)[:16]` |
| Chunk | `chunk_id = f"{doc_id}:{idx:05d}"` |
| Entity | `entity_id = sha1(f"{type}|{normalize(canonical_name)}")[:16]` |
| WikiPage | `page_id = entity_id`, `slug = kebab(canonical_name)` (коллизии → суффикс `-{type}`) |
| Qdrant point | `uuid5(NAMESPACE, <chunk_id \| entity_id \| page_id#section_idx>)` |

`normalize`: lower, NFKC, обрезка пробелов/пунктуации, схлопывание пробелов.

### 4.2 Neo4j

```
(:Document {doc_id, path, title, mime, lang, n_pages, ingested_at})
(:Chunk    {chunk_id, doc_id, idx, text, headings[], page_start, page_end, n_tokens, modality})
(:Entity   {entity_id, name, type, aliases[], description, mention_count})
(:WikiPage {page_id, slug, title, path, content_hash, version, updated_at})

(Document)-[:HAS_CHUNK]->(Chunk)
(Chunk)-[:NEXT]->(Chunk)
(Chunk)-[:MENTIONS]->(Entity)
(Entity)-[:RELATED {predicate, description, weight, evidence: [chunk_id]}]->(Entity)
(WikiPage)-[:ABOUT]->(Entity)
(WikiPage)-[:LINKS_TO]->(WikiPage)
(WikiPage)-[:CITES]->(Chunk)
```

- Unique constraints на `doc_id`, `chunk_id`, `entity_id`, `page_id`.
- Fulltext index `entity_names` на `Entity.name`, `Entity.aliases`.
- Один тип ребра `:RELATED`, семантика — в `predicate` (snake_case). Нормализация к фиксированному словарю — позже при необходимости.
- Типы сущностей задаются в конфиге; по умолчанию: `Person, Organization, Concept, Method, Technology, Dataset, Work, Event, Location, Other`.

### 4.3 Qdrant

Префикс коллекций из конфига (`mb_`).

| Коллекция | Векторы | Payload (индексируемые поля жирным) |
|---|---|---|
| `mb_chunks` | `dense` (dim из конфига, cosine), sparse `bm25` (modifier IDF) | **chunk_id, doc_id, entity_ids[], modality**, text, headings, page, path |
| `mb_entities` | `dense` от `"{name}: {description}"` | **entity_id, type**, name |
| `mb_wiki_pages` | `dense` + `bm25`, точка на секцию `##` | **page_id, entity_id**, section, text |

Метаданные эмбеддера (`model_id`, `dim`) хранятся в registry (`meta`) на коллекцию; при несовпадении с конфигом `ingest`/`ask` отказываются работать и предлагают `mnogobase reindex`.

Промпты эмбеддера задаются шаблонами в конфиге (`embedder.doc_template`, `embedder.query_template`); по умолчанию — EmbeddingGemma 2 (из model card):
- документ: `title: {doc_title | "none"} | text: {chunk}`
- запрос: `task: search result | query: {q}`

Для `qwen3-embedding:0.6b`: `doc_template: "{text}"`, `query_template: "Instruct: Given a question, retrieve passages that answer it\nQuery: {query}"`, `dim: 1024`.

### 4.4 Wiki на диске

```
wiki/
├── index.md              # каталог по типам сущностей (генерируется целиком)
├── log.md                # append-only журнал прогонов
└── entities/<slug>.md
```

Формат страницы:

```markdown
---
id: <entity_id>
type: Method
aliases: [self-attention, механизм внимания]
sources: 4
updated: 2026-10-07
version: 3
---
# Attention Mechanism

Краткое описание… [^a91c…:00012]

## Details
…

## Related
- uses → [[Softmax]]
- part_of → [[Transformer]]

## Sources
[^a91c…:00012]: *attention.pdf*, p.3
```

### 4.5 SQLite registry (`.mnogobase/state.db`)

```
files(path PK, doc_id, size, mtime, status, updated_at)
stages(doc_id, stage, status, attempts, error, updated_at, PK(doc_id, stage))
      stage ∈ parse | chunk | embed | extract | graph ; status ∈ pending | running | done | failed
chunk_extract(chunk_id PK, status, attempts, error)
extraction_cache(chunk_id, prompt_version, model, result_json, PK(chunk_id, prompt_version, model))
dirty_entities(entity_id PK, marked_at)
meta(key PK, value)          # embedder model_id/dim, schema_version
```

Файловый lock на `state.db` запрещает параллельный ingest.

## 5. Поток обработки

### 5.1 `mnogobase ingest <path...> [--no-wiki] [--retry-failed]`

1. **discover** — обход путей, фильтр по поддерживаемым расширениям, `sha256`. По registry:
   - путь+хеш известны и все этапы `done` → skip;
   - путь известен, хеш другой → **cascade delete** старого doc (Qdrant points по `doc_id`; Neo4j Chunk, MENTIONS; удаление `doc`-чанков из `evidence` рёбер; рёбра с пустым `evidence` и сущности с `mention_count = 0` удаляются вместе с wiki-страницами; затронутые сущности → dirty), затем как новый;
   - иначе — продолжить с первого не-`done` этапа.
2. **parse** — Docling `DocumentConverter` (accelerator из `device`, OCR `true|false`). `DoclingDocument` → `.mnogobase/cache/<doc_id>.json`; изображения/рисунки → `.mnogobase/cache/<doc_id>/images/` с подписью и страницей (задел на мультимодальность, не эмбеддятся).
3. **chunk** — `HybridChunker(tokenizer=HF google/embeddinggemma-2, max_tokens=512, merge_peers=True)`. Для эмбеддинга — `contextualize(chunk)`, в payload — чистый текст + headings + страницы.
4. **embed** — dense батчами через Ollama `/api/embed`, sparse BM25 через fastembed → upsert в `mb_chunks`. Создание узлов `Document`, `Chunk`, `HAS_CHUNK`, `NEXT`.
5. **extract** — для каждого чанка (asyncio, семафор `llm.concurrency`) structured output по JSON Schema:
   ```json
   {"entities": [{"name": "", "type": "", "description": "", "aliases": []}],
    "relations": [{"source": "", "target": "", "predicate": "", "description": "", "strength": 1-10}]}
   ```
   Имена — канонические на EN, исходное написание → `aliases`. Результат кэшируется по `(chunk_id, prompt_version, model)`. Неудача чанка не валит документ: `chunk_extract.status = failed`, этап `extract` получает `done` с предупреждением, если доля упавших чанков ≤ `extract.max_failed_ratio` (по умолчанию 0.2), иначе `failed`.
6. **graph** — entity resolution и запись:
   - совпал `entity_id` → та же сущность;
   - иначе поиск в `mb_entities`: cosine ≥ `resolve.auto_merge` (0.92) → merge; в `[resolve.llm_check, auto_merge)` (0.80) → LLM-вопрос «одна ли это сущность?»; ниже → новая;
   - описания аккумулируются; при > `resolve.max_descriptions` (5) LLM сжимает в одно;
   - `MERGE` Entity, `MENTIONS`, `RELATED` по `(src, dst, predicate)` с дописыванием `evidence` и `weight += strength`; upsert `mb_entities`; обновление `entity_ids[]` в payload чанков;
   - затронутые сущности → `dirty_entities`.
7. Если не `--no-wiki` — запуск `wiki build`.

### 5.2 `mnogobase wiki build [--all]`

1. Кандидаты: `dirty_entities` (или все при `--all`) с `mention_count ≥ wiki.min_mentions` (2).
2. Контекст на сущность: описание, aliases, связи (predicate + сосед + описание), top-K (`wiki.evidence_k`, 12) evidence-чанков, ранжированных по близости к эмбеддингу сущности, **текущий текст страницы** (если есть) — LLM дополняет и правит, а не переписывает.
3. LLM возвращает markdown-тело с `[^chunk_id]` и `[[Name]]`.
4. Валидация: цитаты на несуществующие или не входившие в контекст чанки удаляются; `[[Name]]` остаётся ссылкой, только если у Name есть страница (или она создаётся в этом же прогоне), иначе превращается в текст.
5. Запись `.md`, upsert `WikiPage` (`version += 1`, `content_hash`), `ABOUT`, `LINKS_TO`, `CITES`; секции → `mb_wiki_pages`.
6. Пересборка `index.md`, запись в `log.md` (run_id, дата, новые/обновлённые/удалённые страницы, обработанные документы), очистка обработанных `dirty_entities`.

### 5.3 Retrieval: `mnogobase ask "<q>" --mode rag|wiki|graph|all [--k N]`

| Режим | Алгоритм |
|---|---|
| `rag` | Qdrant query с двумя prefetch (`dense`, `bm25`) + fusion RRF по `mb_chunks` → top-k чанков |
| `wiki` | то же по `mb_wiki_pages` → top-k секций; их `[^chunk_id]` разворачиваются в источники |
| `graph` | 1) entity linking: `mb_entities` (вектор запроса) ∪ Neo4j fulltext по именам → seed (top 5); 2) Cypher k-hop (`graph.hops`, 2) с ранжированием по `weight` и степени, лимит `graph.max_relations` (30); 3) контекст = описания сущностей + тройки + evidence-чанки топ-связей |
| `all` | объединение трёх контекстов, дедуп по `chunk_id`, бюджет токенов делится между источниками (`retrieval.budget`) |

**Ответ** (`answer.py`): LLM получает пронумерованные источники `[1]..[n]`, обязан ссылаться `[n]`, отвечает «недостаточно информации», если источников мало. Вывод: ответ + список источников (path, page, chunk_id / page slug).

**`mnogobase compare "<q>"`** — все 4 режима, rich-таблица (ответ, источники, latency, tokens in/out, размер контекста), строка в `runs/compare.jsonl`:
`{ts, question, mode, answer, sources, latency_ms, tokens_in, tokens_out, context_tokens, config_hash}`.

### 5.4 Прочие команды

- `mnogobase doctor` — device, доступность Ollama/Qdrant/Neo4j, наличие моделей (подсказка `ollama pull`), совпадение dim коллекций.
- `mnogobase status` — сводка по registry: документы по этапам, ошибки, время, токены.
- `mnogobase reindex` — пересчёт dense/sparse по текстам из Neo4j (чанки, сущности, wiki-секции) при смене эмбеддера; пересоздаёт коллекции. Граф и wiki не трогает.
- `mnogobase reset [--yes]` — удаление коллекций с префиксом, очистка Neo4j, registry, cache и wiki (с подтверждением).

## 6. Устройства и ускорение

`device: auto` → `cuda` (если `torch.cuda.is_available()` или `nvidia-smi`), иначе `mps`, иначе `cpu`.

| Компонент | CUDA | MPS (Apple) |
|---|---|---|
| Docling | `AcceleratorOptions(device=CUDA)`, увеличенный batch | `MPS` |
| fastembed BM25 | `SparseTextEmbedding(cuda=True)` при установленном `onnxruntime-gpu` (замена `onnxruntime`, см. README) | CPU (BM25 дешёвый) |
| Ollama | сама использует CUDA; `OLLAMA_NUM_PARALLEL` из конфига | Metal |
| Qdrant | `docker compose -f docker-compose.yml -f docker-compose.cuda.yml up -d`: `qdrant/qdrant:v1.19.2-gpu-nvidia` (GPU HNSW indexing) | обычный образ |
| Ollama в Docker | тот же `docker-compose.cuda.yml` (Linux + NVIDIA runtime) | нативно на хосте |
| LLM-сервер | vLLM — через `llm.base_url`, без кода | — |

## 7. Обработка ошибок и восстановление

- Сетевые ошибки/таймауты (LLM, эмбеддер, Qdrant, Neo4j): `tenacity`, экспоненциальный backoff, 5 попыток.
- Невалидный JSON от LLM: до 2 repair-ретраев с текстом ошибки валидации, затем `failed` для чанка.
- Изоляция: ошибка документа не останавливает батч; итог прогона показывает упавшие файлы; `--retry-failed` перезапускает только их.
- Порядок записи: Qdrant → Neo4j → `stages.done`. Все записи — upsert по детерминированным ID, поэтому повтор этапа после падения безопасен.
- Ctrl+C / kill: этапы в `running` при следующем запуске считаются `pending`.
- Pre-flight: `ingest` и `ask` вызывают облегчённый `doctor` и падают с понятным сообщением, если сервис недоступен или dim не совпадает.

## 8. Логирование

- `structlog`: rich-консоль (прогресс-бары) + JSONL `logs/mnogobase.jsonl` (ротация по размеру).
- Общие поля: `ts, level, event, run_id, doc_id, stage, duration_ms`.
- LLM/эмбеддер: `task, model, tokens_in, tokens_out, batch_size, retries`.
- Ошибки: `error_type, error, traceback`.

## 9. Конфигурация по умолчанию

```yaml
device: auto
llm:
  base_url: https://codex.sale/v1   # локально: http://localhost:11434/v1
  model: gpt-6-luna                 # локально: gemma4:26b-a4b
  api_key_env: LLM_API_KEY          # ключ в .env; для Ollama не нужен
  concurrency: 4
  temperature: 0
  timeout_s: 120
  overrides: {extract: null, resolve: null, wiki: null, answer: null}
embedder:
  provider: ollama
  base_url: http://localhost:11434
  model: embeddinggemma-2:740m      # fallback: qwen3-embedding:0.6b (dim 1024, свои шаблоны)
  dim: 768
  batch_size: 32
  doc_template: "title: {title} | text: {text}"
  query_template: "task: search result | query: {query}"
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
  dir: ./wiki
  language: en
  min_mentions: 2
  evidence_k: 12
retrieval:
  k: 8
  budget: {rag: 0.4, wiki: 0.3, graph: 0.3}
graph:
  hops: 2
  max_relations: 30
qdrant:
  url: http://localhost:6333
  prefix: mb_
neo4j:
  uri: bolt://localhost:7687
  user: neo4j
  password_env: NEO4J_PASSWORD
```

## 10. Тестирование

- **Unit** (`pytest`, по умолчанию): `FakeEmbedder` (детерминированные векторы из хеша), `FakeLLM` (canned JSON), in-memory фейки store-интерфейсов. Покрытие: ids/normalize, chunk → payload, extractor (валидация/repair), resolver (пороги merge), cascade delete, registry/resume, wiki validate/render, fusion в `all`, answer citation parsing.
- **Integration** (`-m integration`): `testcontainers` поднимает временные Qdrant и Neo4j — рабочие данные не затрагиваются. Проверяются store-реализации, hybrid query, Cypher k-hop, cascade delete.
- **E2E** (`-m e2e`, нужна Ollama с моделями): фикстуры `tests/fixtures/` (md, docx, pdf; RU и EN, пересекающиеся сущности) → `ingest` → проверки: сущности есть и RU/EN склеены, wiki-страница создана с валидными цитатами, `ask` во всех 4 режимах возвращает цитаты, повторный `ingest` — no-op.
- Инструменты: `uv`, Python 3.12, `ruff` (lint + format).

## 11. Вне скоупа (следующие подпроекты)

- Desktop UI, FastAPI-сервис, MCP-сервер для внешних агентов.
- Deep Research Agent, автоматический поиск knowledge gaps и противоречий.
- Эмбеддинг изображений/аудио (основа заложена: `EmbedInput.modality`, сохранение картинок Docling, поле `modality` в payload); бэкенд `sentence-transformers` для аудио/видео.
- Наблюдаемость: Grafana + Loki (через JSONL-логи), **Langfuse** для трейсинга LLM-вызовов.
- Автоматическая оценка RAG/Wiki/Graph-RAG (LLM-as-judge, датасет вопросов) — `runs/compare.jsonl` проектируется как её вход.
- Watch-папка, фоновая очередь задач, community summaries (GDS Leiden), нормализация предикатов.
- Веб-страницы как источник (URL → HTML → Docling).
