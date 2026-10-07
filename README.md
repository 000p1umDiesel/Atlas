# mnogobase

Ядро проекта **LLM Wiki**. Берёт папку с документами (PDF, DOCX, PPTX, XLSX, HTML, Markdown, TXT, CSV…) и строит из неё:

- **векторный индекс** в Qdrant: плотные векторы EmbeddingGemma 2 + разреженные BM25;
- **граф знаний** в Neo4j: сущности, связи между ними и источник (чанк) для каждого факта;
- **wiki** из Markdown-страниц в стиле Karpathy LLM Wiki — открывается в Obsidian и дописывается при каждом новом документе.

По этим трём слоям можно задавать вопросы и получать ответы со ссылками на источники в четырёх режимах: `rag`, `wiki`, `graph`, `all`.

```
документы ─► Docling (разбор) ─► HybridChunker (чанки) ─► EmbeddingGemma 2 + BM25 ─► Qdrant
                                              └──► LLM: сущности и связи ─► слияние дублей ─► Neo4j
                                                                                   └──► wiki/*.md
вопрос ─► rag | wiki | graph | all ─► ответ с цитатами (+ runs/compare.jsonl)
```

---

## 1. Что нужно установить

| Что | Зачем | Проверка |
|---|---|---|
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) | Qdrant и Neo4j | `docker info` |
| [Ollama](https://ollama.com) | эмбеддер EmbeddingGemma 2 | `ollama --version` |
| [uv](https://docs.astral.sh/uv/) | Python 3.12 и зависимости | `uv --version` |
| [Obsidian](https://obsidian.md) (по желанию) | смотреть wiki и её граф | — |

## 2. Первый запуск (один раз)

Все команды выполняются **из корня репозитория**: оттуда читаются `config.yaml` и `.env`.

```bash
# 1. Секреты
cp .env.example .env
#    откройте .env и впишите LLM_API_KEY=<ваш ключ>
#    NEO4J_PASSWORD оставьте mnogobase-dev (так же он задан в docker-compose.yml)

# 2. Базы данных: Qdrant (:6333) и Neo4j (:7474, :7687)
docker compose up -d

# 3. Модель эмбеддингов (~1.3 ГБ)
ollama pull embeddinggemma-2:740m

# 4. Python-зависимости
uv sync

# 5. Проверка: всё должно быть зелёным
uv run mnogobase doctor
```

`doctor` проверяет устройство (CUDA / MPS / CPU), модель в Ollama, LLM-эндпоинт, Qdrant, Neo4j и совместимость индекса. Если что-то красное, в строке будет написано, что именно не так.

> **Первый запуск `ingest` дольше обычного.** Docling скачивает свои модели разметки и OCR, а чанкер — токенайзер `google/embeddinggemma-2` с Hugging Face. Если Hugging Face откажет в доступе (репозитории Gemma бывают закрыты), примите лицензию Gemma на странице модели и выполните `hf auth login` (или задайте `HF_TOKEN`).

> **Конфиденциальность.** По умолчанию LLM — `gpt-6-luna` на `https://codex.sale/v1`, то есть **текст документов уходит на сторонний сервер**. Для закрытых документов переключитесь на локальную модель Ollama (раздел «Настройка»).

---

## 3. Куда класть документы

Кладите файлы в папку **`documents/`** в корне репозитория (она в `.gitignore`, в git ничего не попадёт):

```bash
mkdir -p documents
cp ~/Downloads/*.pdf documents/
```

- Подпапки можно любые: `ingest` обходит папку рекурсивно.
- Скрытые файлы и папки (начинаются с `.`) пропускаются.
- Поддерживаемые расширения задаются в `config.yaml` → `parsing.extensions`: `pdf, docx, pptx, xlsx, html, htm, md, adoc, csv, txt`.
- Документы могут быть на русском и на английском вперемешку. Страницы wiki пишутся на языке из `wiki.language` (по умолчанию `en`).

Папка может быть любой: `documents/` — просто соглашение. `ingest` принимает и отдельные файлы, и несколько путей сразу.

---

## 4. Загрузка документов: чанкирование, эмбеддинги, граф, wiki

Весь процесс запускает одна команда:

```bash
uv run mnogobase ingest documents/
```

Для каждого файла по очереди выполняются этапы:

| Этап | Что происходит | Где результат |
|---|---|---|
| `parse` | Docling разбирает файл: текст, заголовки, таблицы, OCR для сканов | `.mnogobase/cache/<doc_id>.json`, картинки в `.mnogobase/cache/<doc_id>/images/` |
| `chunk` | HybridChunker режет на чанки до 512 токенов по структуре документа | `.mnogobase/cache/<doc_id>.chunks.json` |
| `embed` | каждый чанк → плотный вектор (EmbeddingGemma 2) + BM25 | Qdrant, коллекция `mb_chunks`; узлы `Document`/`Chunk` в Neo4j |
| `extract` | LLM достаёт из каждого чанка сущности и связи | кэш в `.mnogobase/state.db` |
| `graph` | сущности сливаются с уже известными (в том числе RU↔EN), связи пишутся в граф | Neo4j: `Entity`, `RELATED`, `MENTIONS`; Qdrant `mb_entities` |
| wiki | страницы сущностей, у которых появились новые упоминания, создаются или дописываются | `wiki/entities/*.md`, `wiki/index.md`, `wiki/log.md` |

Как это работает:

- **Инкрементально.** Повторный `ingest` той же папки пропускает неизменённые файлы. Изменённый файл заменяет свою старую версию в Qdrant, Neo4j и wiki без дублей.
- **Возобновляемо.** Если процесс прервался (Ctrl+C, упал сервис), следующий `ingest` продолжит с того этапа, на котором остановился.
- **Ошибки изолированы.** Один сломанный файл не останавливает остальные. В конце выводится сводка; упавшие файлы можно перезапустить командой `uv run mnogobase ingest --retry-failed`.

Полезные варианты:

```bash
uv run mnogobase ingest documents/statya.pdf        # один файл
uv run mnogobase ingest documents/ --no-wiki        # без обновления wiki (быстрее; wiki можно собрать потом)
uv run mnogobase wiki build                         # дописать wiki по накопившимся изменениям
uv run mnogobase wiki build --all                   # перегенерировать все страницы
uv run mnogobase status                             # сколько файлов, этапов, ошибок
```

> Отдельной команды «только чанкирование» нет: чанки получаются на этапе `chunk` внутри `ingest`. Посмотреть их можно в `.mnogobase/cache/<doc_id>.chunks.json`, в Qdrant (раздел 7) или в Neo4j (раздел 6).

---

## 5. Вопросы и сравнение режимов

```bash
uv run mnogobase ask "Что такое механизм внимания?"               # режим all (по умолчанию)
uv run mnogobase ask "Что такое механизм внимания?" --mode graph
uv run mnogobase compare "Что такое механизм внимания?"           # все 4 режима рядом
```

| Режим | Откуда берётся контекст |
|---|---|
| `rag` | ближайшие чанки из Qdrant (гибридный поиск dense + BM25) |
| `wiki` | разделы wiki-страниц и чанки, на которые они ссылаются |
| `graph` | сущности из вопроса, их соседи в графе и чанки-доказательства |
| `all` | всё вместе, в общем бюджете токенов |

В ответе есть таблица источников: файл, страница, `ref` (id чанка или раздела wiki). `compare` дописывает каждый прогон в `runs/compare.jsonl`; на этих данных потом можно сравнивать качество RAG, Wiki и Graph.

---

## 6. Как посмотреть граф в Neo4j (красиво)

1. Откройте **http://localhost:7474**.
2. Подключение: `neo4j://localhost:7687`, пользователь `neo4j`, пароль из `.env` (по умолчанию `mnogobase-dev`).
3. **Стиль (один раз).** Выполните в строке запроса `:style`, затем перетащите в открывшуюся панель файл [`docs/neo4j/mnogobase.grass`](docs/neo4j/mnogobase.grass). У сущностей появятся имена, у связей — предикаты, у документов и чанков — свои цвета.
4. **Цвет по типу сущности (по желанию).** Тип хранится в свойстве `type`; чтобы раскрасить узлы, добавьте его как метку (стиль выше уже содержит цвета для всех типов по умолчанию, см. раздел 11):

   ```cypher
   MATCH (e:Entity) CALL apoc.create.addLabels(e, [e.type]) YIELD node RETURN count(node);
   ```

   Повторяйте после новых `ingest`. mnogobase эти метки не использует и не мешает им.

Готовые запросы (вставлять в строку запроса Neo4j Browser):

```cypher
// Весь граф знаний: сущности и связи между ними (самые сильные связи)
MATCH (a:Entity)-[r:RELATED]->(b:Entity)
RETURN a, r, b ORDER BY r.weight DESC LIMIT 300;
```

```cypher
// Окрестность одной сущности на 2 шага (замените имя)
MATCH (e:Entity) WHERE toLower(e.name) CONTAINS 'transformer'
MATCH p = (e)-[:RELATED*1..2]-(:Entity)
RETURN p LIMIT 150;
```

```cypher
// Самые упоминаемые сущности
MATCH (e:Entity)
RETURN e.name AS name, e.type AS type, e.mention_count AS mentions, e.aliases AS aliases
ORDER BY mentions DESC LIMIT 30;
```

```cypher
// Документ → его чанки → сущности, которые в них упоминаются
MATCH (d:Document)-[:HAS_CHUNK]->(c:Chunk)-[:MENTIONS]->(e:Entity)
WHERE d.path CONTAINS 'statya'          // часть имени файла
RETURN d, c, e LIMIT 200;
```

```cypher
// Чанки одного документа по порядку (как его порезал чанкер)
MATCH (d:Document)-[:HAS_CHUNK]->(c:Chunk)
WHERE d.path CONTAINS 'statya'
RETURN c.idx AS idx, c.headings AS headings, c.n_tokens AS tokens, left(c.text, 200) AS text
ORDER BY idx;
```

```cypher
// Откуда взят факт: связь и тексты чанков-доказательств
MATCH (a:Entity)-[r:RELATED]->(b:Entity)
WHERE toLower(a.name) CONTAINS 'transformer'
UNWIND r.evidence AS cid
MATCH (c:Chunk {chunk_id: cid})<-[:HAS_CHUNK]-(d:Document)
RETURN a.name, r.predicate, b.name, d.path, c.page_start, left(c.text, 200) LIMIT 50;
```

```cypher
// Wiki-страницы и ссылки между ними
MATCH (p:WikiPage)-[l:LINKS_TO]->(q:WikiPage) RETURN p, l, q LIMIT 200;
```

Схема графа: `(:Document)-[:HAS_CHUNK]->(:Chunk)-[:NEXT]->(:Chunk)`, `(:Chunk)-[:MENTIONS]->(:Entity)`, `(:Entity)-[:RELATED {predicate, weight, evidence}]->(:Entity)`, `(:WikiPage)-[:ABOUT]->(:Entity)`, `(:WikiPage)-[:LINKS_TO]->(:WikiPage)`, `(:WikiPage)-[:CITES]->(:Chunk)`.

---

## 7. Как посмотреть векторы и чанки в Qdrant

Откройте **http://localhost:6333/dashboard** → **Collections**:

- `mb_chunks` — чанки документов (payload: `text`, `path`, `page`, `headings`, `entity_ids`);
- `mb_entities` — сущности (для слияния дублей);
- `mb_wiki_pages` — разделы wiki-страниц.

В коллекции есть вкладка **Points** (содержимое и payload) и **Visualize** (2D-проекция векторов: видно, как чанки группируются по темам).

---

## 8. Как смотреть wiki в Obsidian

1. В Obsidian: **Open folder as vault** → выберите папку **`wiki/`** в корне репозитория. Она появится после первого `ingest`.
2. Начните с **`index.md`**: это оглавление всех страниц по типам сущностей. В **`log.md`** — журнал: какие документы обработаны и какие страницы созданы, обновлены или удалены.
3. Страницы сущностей лежат в `entities/`. Каждая страница содержит:
   - свойства (frontmatter): `type`, `aliases`, `sources`, `updated`, `version`. `aliases` Obsidian использует при поиске и в подсказках ссылок, поэтому русские и английские названия находят одну страницу;
   - текст со сносками `[^chunk_id]`: каждое утверждение ссылается на чанк-источник, а внизу в разделе **Sources** указаны файл и страница;
   - раздел **Related**: связи из графа с wiki-ссылками `[[slug|Имя]]`.
4. **Граф:** `Cmd/Ctrl+G` открывает общий граф, «Open local graph» в меню страницы — окрестность текущей страницы.
5. **Раскраска по типам:** в настройках графа → **Groups** → «New group» и запрос по свойству, например:
   - `[type:Person]` — люди,
   - `[type:Concept]` — понятия,
   - `[type:Method]` — методы,
   - `[type:Technology]` — технологии.

   Чтобы скрыть служебные страницы, в **Filters** укажите `-file:index -file:log`.

Что важно знать:

- **Не редактируйте `entities/*.md` вручную:** при следующем обновлении страницы LLM перепишет её на основе прежнего текста и новых фактов, ручные правки могут потеряться. Свои заметки кладите рядом, например в `wiki/notes/`. Их не трогает ни `wiki build`, ни `reset`. В заметках можно ссылаться на страницы сущностей: `[[transformer]]`.
- Ссылка на страницу ставится только если страница существует. Страница создаётся для сущностей, у которых не меньше `wiki.min_mentions` упоминаний (по умолчанию 2).
- Папка `wiki/` в `.gitignore` (это производные данные). Если команде нужно хранить wiki в git, уберите её оттуда.

---

## 9. Как потестить

**Быстрая ручная проверка на встроенных примерах** (небольшой английский и русский тексты про механизм внимания):

```bash
docker compose up -d
uv run mnogobase doctor
uv run mnogobase ingest tests/fixtures/
uv run mnogobase status
uv run mnogobase compare "What is multi-head attention?"
```

Потом откройте граф (раздел 6) и `wiki/` в Obsidian (раздел 8). Сущности из русского и английского текстов должны слиться в общие: у них появятся алиасы на обоих языках (точный результат зависит от ответа LLM).

Чтобы начать с чистого листа:

```bash
uv run mnogobase reset            # покажет, что удалит, и спросит подтверждение
```

**Автотесты:**

```bash
uv run pytest                    # модульные (~10 с, без сервисов)
uv run pytest -m integration     # интеграционные: сами поднимают Neo4j и Qdrant в Docker (testcontainers)
uv run pytest -m e2e -s          # сквозной: Docker + Ollama (embeddinggemma-2:740m) + LLM (LLM_API_KEY в .env); тратит запросы к LLM
uv run ruff check . && uv run ruff format --check .
```

Интеграционные и e2e-тесты используют **свои временные контейнеры**: ваши данные в `docker compose` они не трогают.

---

## 10. Все команды

Глобальная опция `--config/-c PATH` ставится **перед** командой: `uv run mnogobase -c other.yaml status`. По умолчанию используется `./config.yaml` или путь из `$MNOGOBASE_CONFIG`.

| Команда | Что делает |
|---|---|
| `doctor` | проверяет устройство, модель Ollama, LLM, Qdrant, Neo4j, совместимость индекса (код выхода 1 при ошибке); смена типов сущностей — только предупреждение |
| `status` | файлы по статусам, состояние этапов, сущности в очереди на обновление wiki, незавершённые удаления, ошибки |
| `ingest PATH... [--no-wiki] [--retry-failed]` | загрузка документов (раздел 4) |
| `wiki build [--all]` | обновить страницы сущностей с новыми упоминаниями (или все) |
| `ask "ВОПРОС" [--mode/-m rag\|wiki\|graph\|all] [--k N]` | ответ с цитатами из одного режима |
| `compare "ВОПРОС" [--k N]` | все 4 режима рядом + запись в `runs/compare.jsonl` |
| `reindex` | пересчитать все векторы после смены эмбеддера (граф и wiki сохраняются) |
| `reset [--yes]` | удалить все данные проекта (см. ниже) |

`ingest`, `wiki build`, `ask`, `compare`, `reindex` и `reset` сначала проверяют нужные сервисы. Если сервис недоступен, команда останавливается и предлагает запустить `doctor`. Одновременно работает только одна из команд `ingest` / `wiki build` / `reindex` / `reset` (блокировка в `.mnogobase/`).

**`reset` безопасен:**

- вне проекта (нет `.mnogobase/state.db`) он отказывается работать даже с `--yes`;
- перед подтверждением показывает, что именно удалит: все узлы в базе Neo4j (держите отдельный Neo4j на проект), коллекции Qdrant с префиксом `mb_`, состояние и кэши в `.mnogobase/`, а из wiki — только то, что создал сам: `entities/`, `index.md`, `log.md`.

---

## 11. Настройка

Все несекретные параметры — в `config.yaml`, секреты — в `.env`. Любой параметр можно переопределить переменной окружения: `MNOGOBASE_LLM__MODEL=gemma4:26b-a4b`, `MNOGOBASE_EMBEDDER__DIM=512`.

- **LLM** — любой OpenAI-совместимый эндпоинт. По умолчанию `https://codex.sale/v1`, модель `gpt-6-luna`.
  - Локально через Ollama: `llm.base_url: http://localhost:11434/v1`, `llm.model: gemma4:26b-a4b`. Ключ тогда не нужен.
  - Отдельные модели под задачи: `llm.overrides.{extract,resolve,wiki,answer}`.
- **Эмбеддер** — Ollama `embeddinggemma-2:740m`, 768 измерений (Matryoshka: можно 512/256).
  - Запасной вариант: `ollama pull qwen3-embedding:0.6b` и раскомментировать блок в `config.yaml` (`dim: 1024` и свои шаблоны).
  - После смены модели или размерности выполните `uv run mnogobase reindex`. `ingest` и `ask` не дают смешивать векторы разных эмбеддеров.
- **Чанкирование** — `chunking.max_tokens` (по умолчанию 512).
- **Wiki** — `wiki.language` (`en`), `wiki.min_mentions` (2), `wiki.evidence_k` (12 чанков-доказательств на страницу).
- **Поиск** — `retrieval.k`, `retrieval.context_tokens`, доли бюджета для режима `all` в `retrieval.budget`.
- **Сущности** — типы в `extract.entity_types`, см. ниже.

Логи в формате JSON по строкам пишутся в `logs/mnogobase.jsonl` (поля `run_id`, `doc_id`, `stage`, `duration_ms` и др.).

### Типы сущностей

Типы задаются в `extract.entity_types`: имя типа → одна строка описания. Описания попадают в промпт извлечения и помогают LLM различать похожие типы. По умолчанию 20 типов для документов из любых областей, не только научных статей:

`Person`, `Organization`, `Location`, `Event`, `Project`, `Product`, `Software`, `Technology`, `AIModel`, `Method`, `Concept`, `Field`, `Work`, `Dataset`, `Metric`, `Regulation`, `Substance`, `Condition`, `Organism`, `Other`.

Чтобы добавить или изменить тип, отредактируйте список в `config.yaml` (порядок сохраняется, `Other` оставляйте последним: в него попадает всё, что LLM отнесла к неизвестному типу):

```yaml
extract:
  entity_types:
    Person: A real or fictional individual, named or clearly identified.
    Gene: A named gene or protein; not the disease it causes (Condition).
    Condition: A disease, disorder, symptom or other medical or psychological condition.
    Other: Anything meaningful that fits none of the types above.
```

Можно указать и просто список имён без описаний: `entity_types: [Person, Gene, Other]`.

Главное правило: **типы не должны пересекаться**. Тип входит в идентификатор сущности (`entity_id`), поэтому если одну и ту же вещь можно отнести к двум типам, она расколется на две разные сущности (например, `PyTorch` как `Software` и как `Technology`). В описании полезно прямо писать, чем тип отличается от соседних.

Что происходит после изменения типов:

- изменением считается любая правка списка: новое или удалённое имя, другое описание и даже другой порядок типов (меняется промпт);
- новые и изменённые документы сразу извлекаются с новыми типами (кэш извлечения учитывает набор типов);
- уже загруженные документы сохраняют старые типы. `status`, `doctor` и `ingest` предупреждают об этом, пока остаётся хотя бы один документ, извлечённый со старыми типами;
- чтобы переизвлечь всё с новыми типами: `uv run mnogobase reset`, затем `uv run mnogobase ingest documents/` (это заново вызывает LLM для всех чанков).

Для раскраски новых типов в Neo4j добавьте строку вида `node.Gene { color: #...; border-color: #...; }` в [`docs/neo4j/mnogobase.grass`](docs/neo4j/mnogobase.grass) (раздел 6).

## 12. GPU / CUDA

`device: auto` выбирает CUDA → MPS (Apple Silicon) → CPU для моделей Docling. На Linux с NVIDIA (нужен NVIDIA Container Toolkit):

```bash
docker compose -f docker-compose.yml -f docker-compose.cuda.yml up -d   # Qdrant с GPU-индексацией + Ollama в Docker
docker compose exec ollama ollama pull embeddinggemma-2:740m
uv pip uninstall onnxruntime && uv pip install onnxruntime-gpu          # BM25 на GPU
```

После замены на `onnxruntime-gpu` запускайте команды через `uv run --no-sync ...`: иначе `uv` вернёт CPU-версию из lock-файла.

## 13. Структура кода

`src/mnogobase/`:

- `parsing/` — Docling;
- `chunking/` — HybridChunker;
- `embedding/` — Ollama и BM25;
- `llm/` — клиент и промпты;
- `extraction/` — извлечение сущностей и их слияние;
- `stores/` — Qdrant и Neo4j;
- `wiki/` — сборка и рендер страниц;
- `retrieval/` — 4 режима поиска;
- `registry.py` — состояние в SQLite;
- `pipeline.py` — этапы `ingest`;
- `app.py` — сборка компонентов;
- `doctor.py`, `maintenance.py` (`reindex`, `reset`), `cli.py`.

Дизайн: [`docs/superpowers/specs/2026-10-07-mnogobase-ingest-design.md`](docs/superpowers/specs/2026-10-07-mnogobase-ingest-design.md).

## 14. Что дальше (вне этого этапа)

- Desktop UI, сервер FastAPI/MCP, агент Deep Research.
- Поиск пробелов и противоречий в знаниях.
- Эмбеддинги картинок и аудио (Docling уже сохраняет картинки в `.mnogobase/cache/<doc_id>/images/`).
- Langfuse-трейсинг, автоматическое сравнение RAG / Wiki / Graph по `runs/compare.jsonl`.
- Команда удаления из индекса файлов, удалённых с диска.
