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
cp .env.example .env               # then set LLM_API_KEY=... in .env
docker compose up -d               # Qdrant :6333, Neo4j :7474/:7687
ollama pull embeddinggemma-2:740m
uv sync
uv run mnogobase doctor
uv run mnogobase ingest ./docs
uv run mnogobase ask "What is attention?" --mode graph
uv run mnogobase compare "What is attention?"
```

`.env` holds the secrets and is git-ignored; mnogobase reads it from the current directory, so
run commands from the project root:

- `LLM_API_KEY` — key for the LLM endpoint (not needed for a local Ollama LLM).
- `NEO4J_PASSWORD` — must match the password Docker Compose gave Neo4j. Both default to
  `mnogobase-dev` (`.env.example` and `docker-compose.yml`). Without `.env` the client sends an
  empty password and Neo4j authentication fails. If you change it after Neo4j was first started,
  recreate the `neo4j_data` volume — Neo4j keeps the password it was initialized with.

Neo4j Browser: http://localhost:7474 (user `neo4j`, password from `.env`). Wiki: open `wiki/` in Obsidian.

## Commands

All commands take a global `--config/-c PATH` (default `./config.yaml`, or `$MNOGOBASE_CONFIG`),
placed before the command: `uv run mnogobase -c other.yaml status`.

| Command | What it does |
|---|---|
| `doctor` | checks device, Ollama model, LLM endpoint, Qdrant, Neo4j, index signature (exit 1 on any failure) |
| `status` | files by status, stage states, entities waiting for a wiki update, failed stages with errors |
| `ingest PATH... [--no-wiki] [--retry-failed]` | incremental ingest; unchanged files are skipped, changed files replace their old version. `--retry-failed` without paths retries every failed file. The summary lists failed files and failed wiki pages (exit 1 if any) |
| `wiki build [--all]` | regenerate pages of entities with new mentions (or all) |
| `ask "Q" [--mode/-m rag\|wiki\|graph\|all] [--k N]` | cited answer from one mode (default `all`) |
| `compare "Q" [--k N]` | all four modes side by side, appended to `runs/compare.jsonl` |
| `reindex` | re-embed everything after changing the embedder (graph and wiki are kept) |
| `reset [--yes]` | delete all data of this project (see below) |

`ingest`, `ask`, `compare`, `reindex` and `reset` first check the services they need and stop
with a hint to run `doctor` if one is down. Only one `ingest` / `wiki build` / `reindex` / `reset`
runs at a time per project (file lock in `.mnogobase/`).

**`reset` safety.** It refuses to run outside a mnogobase project (no `state.db` in the
configured `data_dir`, default `.mnogobase/`), even with `--yes`. Before asking for confirmation it lists exactly what will
be deleted: all nodes in the Neo4j database (use a dedicated Neo4j per project), the Qdrant
collections with this project's prefix, the state and caches in `.mnogobase/`, and only the wiki
files mnogobase owns — `wiki/entities/`, `wiki/index.md`, `wiki/log.md`. Your own notes in
`wiki/` are kept.

## Configuration

`config.yaml` holds all non-secret settings; secrets go to `.env`. Any value can be overridden
with env vars, e.g. `MNOGOBASE_LLM__MODEL=gemma4:26b-a4b`, `MNOGOBASE_EMBEDDER__DIM=512`.

- **LLM** — any OpenAI-compatible endpoint (default `https://codex.sale/v1`, model `gpt-6-luna`).
  Local: `llm.base_url: http://localhost:11434/v1`, `llm.model: gemma4:26b-a4b`.
  Per-task models: `llm.overrides.{extract,resolve,wiki,answer}`.
- **Embedder** — Ollama `embeddinggemma-2:740m` (`ollama pull embeddinggemma-2:740m`), 768d
  (Matryoshka: 512/256 also work). Fallback if EmbeddingGemma 2 is unavailable:
  `ollama pull qwen3-embedding:0.6b` and in `config.yaml` set `embedder.model: qwen3-embedding:0.6b`,
  `embedder.dim: 1024`, `embedder.doc_template: "{text}"`,
  `embedder.query_template: "Instruct: Given a question, retrieve passages that answer it\nQuery: {query}"`
  (the commented block in `config.yaml`). After changing model or dim run `mnogobase reindex` —
  `ingest`/`ask` refuse to mix vectors from different embedders.
- **Wiki** — `wiki.language` (default `en`), `wiki.min_mentions` (entities with fewer mentions get no page).

## GPU / CUDA

`device: auto` picks CUDA → MPS → CPU for Docling layout/OCR models. On Linux with NVIDIA
(NVIDIA Container Toolkit installed):

```bash
docker compose -f docker-compose.yml -f docker-compose.cuda.yml up -d   # GPU Qdrant indexing + Ollama in Docker
uv pip uninstall onnxruntime && uv pip install onnxruntime-gpu          # GPU BM25 in fastembed
```

With the CUDA override Ollama runs in Docker on port 11434, so pull the embedder there:
`docker compose exec ollama ollama pull embeddinggemma-2:740m`.
For a faster LLM on CUDA, serve a model with vLLM and point `llm.base_url` at it.

## Tests

```bash
uv run pytest                    # unit tests
uv run pytest -m integration     # needs Docker (testcontainers: Neo4j, Qdrant)
uv run pytest -m e2e -s          # needs Docker + Ollama (embeddinggemma-2:740m) + LLM endpoint (LLM_API_KEY in .env)
```

The first run that chunks documents (the e2e test, or the first `ingest`) downloads the
`google/embeddinggemma-2` tokenizer from Hugging Face. Gemma repositories may be gated: if the
download is refused, accept the Gemma license on the model's Hugging Face page and log in with
`hf auth login` (or set `HF_TOKEN`). Docling also downloads its layout/OCR models on first use.

## Layout

`src/mnogobase/`: `parsing/` (Docling), `chunking/`, `embedding/`, `llm/` (client + prompts),
`extraction/` (extractor, entity resolver), `stores/` (Qdrant, Neo4j), `wiki/`, `retrieval/`,
`registry.py` (SQLite state), `pipeline.py`, `app.py` (wiring), `doctor.py`, `maintenance.py`
(reindex, reset), `cli.py`. Design: `docs/superpowers/specs/2026-10-07-mnogobase-ingest-design.md`.

## Next steps (out of scope here)

Desktop UI, FastAPI/MCP server, Deep Research agent, knowledge-gap and contradiction detection,
image/audio embedding (Docling already saves pictures to `.mnogobase/cache/<doc_id>/images/`),
Grafana/Loki dashboards, Langfuse tracing, automatic RAG vs Wiki vs Graph evaluation on
`runs/compare.jsonl`.
