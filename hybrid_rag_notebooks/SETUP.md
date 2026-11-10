# Как запустить

[← обратно к README](README.md)

Понадобятся Python 3.12, Docker и [llama.cpp](https://github.com/ggml-org/llama.cpp) (`llama-server`), а также доступ к какой-нибудь OpenAI-совместимой LLM для ответов и оценки. GPU не обязателен: Docling работает на CPU, а маленькие модели для embeddings, reranker и VLM нормально идут через Metal на Mac или вообще на процессоре.

Все команды ниже выполняются из этой папки.

## 1. Python

```bash
python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
```

В `.env` нужно заполнить три строки `LLM_*`: адрес API, имя модели и ключ. Остальное можно не трогать, если модели запускаются локально на портах по умолчанию.

## 2. Qdrant

```bash
docker compose up -d
```

Qdrant поднимется на `localhost:6333`, данные лягут в Docker volume и переживут перезапуск. `docker compose down` останавливает контейнер, база при этом сохраняется. Если порт занят, поменяйте `QDRANT_PORT` и `QDRANT_URL` в `.env`.

## 3. Локальные модели

Каждую команду запускайте в своём терминале. При первом запуске `-hf` скачивает GGUF с Hugging Face, потом берёт его из кеша.

```bash
# embeddings, нужны во всех трёх ноутбуках
llama-server -hf mradermacher/Qwen3-Embedding-0.6B-GGUF:Q8_0 --alias mradermacher/Qwen3-Embedding-0.6B-GGUF:Q8_0 \
  --embedding --pooling last --batch-size 8192 --ubatch-size 8192 \
  --host 127.0.0.1 --port 8101 --ctx-size 8192 --parallel 1 --n-gpu-layers 999

# reranker, нужен в 02
llama-server -hf gorse-io/Qwen3-Reranker-0.6B-GGUF:Q8_0 --alias gorse-io/Qwen3-Reranker-0.6B-GGUF:Q8_0 \
  --embedding --rerank --pooling rank --batch-size 8192 --ubatch-size 8192 \
  --host 127.0.0.1 --port 8102 --ctx-size 8192 --parallel 1 --n-gpu-layers 999

# VLM для описания рисунков, нужна только в 01
llama-server -hf prithivMLmods/Xiaomi-OCR-0-GGUF:Q6_K --alias prithivMLmods/Xiaomi-OCR-0-GGUF:Q6_K \
  --image-min-tokens 1024 \
  --host 127.0.0.1 --port 8103 --ctx-size 8192 --parallel 1 --n-gpu-layers 999
```

Те же команды печатают первые ячейки ноутбуков 01 и 02, уже с адресами из вашего `.env`.

Несколько замечаний:
- `--alias` должен совпадать с `*_MODEL` в `.env`: ноутбуки проверяют имя модели через `/v1/models`;
- reranker нужен именно от `gorse-io`: в этой конвертации сохранён классификатор, а другие GGUF Qwen3-Reranker загружаются, но выдают бессмысленные оценки;
- `--image-min-tokens 1024` для VLM обязателен. Без него Xiaomi-OCR на мелких рисунках из PDF придумывает содержимое;
- без GPU замените `--n-gpu-layers 999` на `0`, а для VLM добавьте `--no-mmproj-offload`.

Модель готова, когда `curl localhost:8101/health` отвечает `{"status":"ok"}`. Ответ 503 означает, что модель ещё загружается.

## 4. Ноутбуки

```bash
jupyter lab
```

Открывайте по порядку: `01_indexing` → `02_retrieval_and_answer` → `03_evaluation`. Каждый ноутбук работает в своём kernel, данные между ними передаются через `outputs/` и Qdrant. Если после изменения `.env` что-то пошло не так, перезапустите kernel: переменные читаются при старте.

Что сколько стоит при полном прогоне:

| ноутбук | время | вызовы LLM |
|---|---|---|
| 01 | 1–2 мин в первый раз (OCR + VLM, без учёта скачивания моделей), потом секунды | нет |
| 02 | ~7 мин | 42: два демонстрационных ответа и 40 ответов основной конфигурации |
| 03 | ~5 мин | ~45 оценок Ragas (40 в основном расчёте и несколько в разборе), каждая — несколько запросов к судье |

Долгие ячейки сохраняют результат построчно. Если прервать ячейку и запустить снова, она продолжит с того места, где остановилась.

## Судья

По умолчанию судья в 03 — та же модель, что и генератор. Чтобы проверять другой моделью, заполните `JUDGE_BASE_URL`, `JUDGE_MODEL`, `JUDGE_API_KEY` и задайте новый `EVAL_NAME` в 03. Модель должна уметь отвечать структурированным JSON: Ragas работает через instructor. `JUDGE_EMBED_*` задают embeddings для Answer Relevancy; если их не заполнять, используется сервер на 8101.

## Свои документы

Положите файлы в `data/documents/` (PDF, DOCX, PPTX, HTML, Markdown, картинки), в 01 пропустите ячейку со скачиванием статьи и задайте новый `INDEX_NAME`. Вопросы пишутся в JSONL по одному на строку:

```json
{"id": "q1", "question": "Сколько голов внимания в базовой модели?", "reference": "8", "source_pages": [5]}
```

`reference` (эталон) и `source_pages` (страницы с ответом) нужны только для оценки, в поиск они не передаются. Блок с отбором вопросов для Ragas в 03 придётся поправить под свой набор.

## Тесты

```bash
python -m unittest discover -s tests -v
```

Тесты не ходят в сеть и не вызывают модели. Они проверяют helpers и функции поиска из ячеек 02 на Qdrant в памяти, а также то, что в сохранённых выводах ноутбуков нет ошибок и локальных путей.

## Если не работает

| что видно | что проверить |
|---|---|
| `Connection refused` | запущен ли сервер, совпадает ли порт с `.env` |
| `нет модели '…'` | `--alias` и `*_MODEL` в `.env` должны совпадать |
| 401 / 403 | ключ в `.env`; после правки перезапустить kernel |
| `настройки отличаются от сохранённых` | поменяли параметры, но оставили старое `INDEX_NAME` / `RUN_NAME` |
| `индекс неполный` в 02 | 01 не дошёл до конца, перезапустите его |
| OCR выдаёт мусор на кириллице | в `OcrAutoOptions` должен стоять `iso:ru` первым языком |
