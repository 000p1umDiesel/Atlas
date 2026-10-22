# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`mnogobase` is the core of an "LLM Wiki" project: a Python 3.12 package and Typer CLI that ingests RU/EN documents into three linked knowledge layers (Qdrant vectors, a Neo4j graph, a Markdown wiki in `wiki/`) and answers questions with cited sources. `README.md` (in Russian) is the detailed reference for internals, config keys and troubleshooting. Keep it in sync when behavior changes.

## Commands

Run everything from the repo root: `config.yaml` and `.env` are read from the CWD, and `.mnogobase/`, `wiki/`, `logs/`, `runs/` are relative to it.

```bash
uv sync                                   # install deps (editable) into .venv
docker compose up -d                      # Qdrant :6333, Neo4j :7474/:7687 (docker-compose.cuda.yml for GPU)
scripts/llama-local.sh start [llm]        # Mac: native llama-server, embedder :8001, reranker :8002 (+ LLM :8000)
uv run mnogobase doctor                   # check device, models, tokenizer, llm, qdrant, neo4j, index, types

uv run mnogobase ingest tests/fixtures/   # small RU+EN sample corpus
uv run mnogobase status | wiki build [--all] | reindex | reset [--yes]
uv run mnogobase ask "..." -m rag|wiki|graph|all
uv run mnogobase compare "..."            # all modes side by side, appends to runs/compare.jsonl
uv run mnogobase -c other.yaml status     # global --config goes BEFORE the subcommand

uv run pytest                             # unit tests only (addopts excludes integration/e2e)
uv run pytest tests/unit/test_cli.py::test_name
uv run pytest -m integration              # needs Docker; testcontainers spin up their own Neo4j + Qdrant
uv run pytest -m e2e -s                   # Docker + llama.cpp embedder/reranker + real LLM (spends LLM calls)
uv run ruff check . && uv run ruff format --check .
```

Any config key can be overridden with `MNOGOBASE_<SECTION>__<KEY>` env vars (lists and dicts as JSON). Secrets (`LLM_API_KEY`, `NEO4J_PASSWORD`) live in `.env`.

## Architecture

**Wiring.** `cli.py` → `app.py:build_app(settings, *, embedder, sparse, llm, vectors, graph)` builds and connects every component. Anything not passed in is created from settings. This is the single injection point that tests use to pass in fakes. Retrieval modes come from `retrieval/__init__.py:build_retrievers` as `{mode: Retriever}`.

**Swappable parts are `typing.Protocol`s:** `Embedder` / `SparseEncoder` (`embedding/base.py`), `Reranker` (`retrieval/rerank.py`, optional, used by `RagRetriever`), `LLMClient` (`llm/client.py`, with `task` ∈ extract/resolve/wiki/answer/query selecting the model via `llm.overrides`) and `Retriever` (`retrieval/base.py`; `alt_queries` carries the English translation of a non-English question from `retrieval/translate.py`). Stores, `Registry`, `DoclingParser` and `Chunker` are concrete classes. Embedder (`embedding/openai_compat.py`), reranker (`ApiReranker`) and LLM all talk to OpenAI-compatible servers (`/embeddings`, `/rerank`, `/chat/completions`; `base_url` includes `/v1`): llama.cpp server by default (`scripts/llama-local.sh` on Mac, `docker-compose.llamacpp.yml` on a server), vLLM via `docker-compose.vllm.yml`. One model per server process, ports 8000 LLM / 8001 embedder / 8002 reranker; on llama.cpp the model name in config is its `--alias`. All Qdrant code lives in `stores/qdrant_store.py`, and all Cypher in `stores/graph_store.py`.

**Ingest pipeline** (`pipeline.py`): runs per document through `parse → chunk → embed → extract → graph`, then an incremental `wiki build`. Per-stage status lives in the SQLite registry (`.mnogobase/state.db`, `registry.py`). A stage is marked `done` only after all of its writes. `running` stages left by a crash reset to `pending` on the next run, and `failed` stages only rerun with `--retry-failed`. Writing commands share a file lock (`.mnogobase/ingest.lock`).

**Invariants to preserve:**
- **Deterministic IDs** (`ids.py`) make every write an idempotent upsert/`MERGE`: `doc_id = sha256(file bytes)[:16]`, `chunk_id = f"{doc_id}:{idx:05d}"`, `entity_id` from type + `normalize_name(name)`, `page_id = entity_id`, Qdrant point id = `uuid5(key)`.
- **Write order:** vector → Qdrant → Neo4j, so a failing embedder or Qdrant never leaves the graph ahead. Removing an old version of a changed file is journaled in `pending_removals` and finished first on the next `ingest` / `wiki build`.
- **Embedder signature** `model_id:dim:tpl-<hash of templates>` is stored in registry `meta`. Changing the embedder model, dim or templates blocks ingest/ask until `mnogobase reindex`.
- **Extraction cache** key is `(chunk_id, prompt_version, model)`. Bump `PROMPT_VERSION` in `extraction/extractor.py` whenever you change the meaning of `llm/prompts/extract.md`.
- **Neo4j schema** lives in the `_SCHEMA` list in `graph_store.py` (`IF NOT EXISTS`, applied on every start). New per-document data must be handled in `_delete_document_tx` and `document_deletion_plan`.

**Entity resolution** (`extraction/resolver.py`): an exact `entity_id` match merges. Otherwise it vector-searches `mb_entities`: a score ≥ `resolve.auto_merge` merges, and scores between `llm_check` and `auto_merge` go to the `resolve_same.md` LLM check. Entities are extracted under canonical English names, with the original spelling kept as an alias, so RU and EN texts converge.

**Wiki** (`wiki/`): only entities in the `dirty_entities` queue are rebuilt. The LLM updates the existing page text. `validate.py` drops citations to chunks outside the evidence and `[[links]]` to pages that don't exist. `## Related` / `## Sources` and the footnote definitions are generated by code, not by the LLM.

**Prompts** live in `src/mnogobase/llm/prompts/*.md` and use `string.Template` (`$name`, literal `$$`). A new placeholder must be passed at the `render(...)` call site and covered in `tests/unit/test_templates.py`. JSON response shapes come from pydantic schemas in code, not from the prompts.

## Testing conventions

- `tests/fakes.py`: `FakeLLM(handler)` (with `scripted_llm_handler` for keyword-scripted replies), `FakeEmbedder` and `FakeSparse` (hashed bag of words).
- Unit tests use `QdrantClient(":memory:")`. There is no in-memory Neo4j, so anything touching the graph is an `integration` test (fixtures `qdrant_client`, `neo4j_container`, `graph` in `tests/conftest.py`).
- CLI tests use `typer.testing.CliRunner`, with `run_checks` and `configure_logging` patched by a fixture.
- New CLI commands follow the existing helpers in `cli.py`: `_settings(project=True)`, `_preflight(settings, checks)`, `_lock(settings)` for writers, `application.close()` in `finally`, `_fail_cleanly` for known errors (exit code 2), and `escape(...)` for any document text printed through rich.

## Environment notes

- On Windows with a system proxy (registry), httpx routes even `localhost` through it, which shows up as 503 from llama.cpp/Qdrant. `config.load_settings` adds local hosts to `NO_PROXY` to prevent this. Code paths that build `Settings` without `load_settings` (some tests) are not covered.
- `config.yaml` uses Qwen3-Embedding-4B (alias `qwen3-embedding-4b`, dim 2560) and Qwen3-Reranker-4B (`qwen3-reranker-4b`, a GGUF converted with the classifier head, e.g. `gorse-io/...`; older conversions load but score garbage) on llama.cpp, and `chunking.tokenizer: auto`: the tokenizer comes from the embedder's server (`root` in vLLM's `/v1/models`, else llama.cpp `/tokenize` via `ServerTokenizer` in `chunking/tokenizer.py`). The index signature is `openai:<model>:<dim>:tpl-...`; indexes from the Ollama era (`ollama:...`) need `reindex`.
- Commit prefixes: `feat:`, `fix:`, `docs:`. Derived data (`.mnogobase/`, `logs/`, `runs/`, `wiki/`, `documents/`) is gitignored.
