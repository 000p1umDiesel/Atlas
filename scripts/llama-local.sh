#!/usr/bin/env bash
# Эмбеддер и реранкер на нативном llama.cpp server (Mac: Metal; Linux без Docker: CUDA/CPU) —
# те же модели, alias и порты, что в docker-compose.llamacpp.yml, поэтому config.yaml один.
#   brew install llama.cpp            # или сборка llama.cpp с llama-server в PATH
#   scripts/llama-local.sh start      # 8001 эмбеддер, 8002 реранкер; модели качаются с HF сами
#   scripts/llama-local.sh start llm  # + локальная LLM на 8000
#   scripts/llama-local.sh status | stop
# Модели меняются переменными LLAMACPP_EMBED_MODEL, LLAMACPP_RERANK_MODEL, LLAMACPP_LLM_MODEL
# (repo:quant на Hugging Face) вместе с их LLAMACPP_*_ALIAS: alias — это имя модели в config.yaml,
# а оно входит в подпись индекса и ключ кэша извлечения. Кэш GGUF — LLAMA_CACHE, иначе кэш
# Hugging Face (~/.cache/huggingface/hub). Сервер слушает только localhost.
set -euo pipefail

cd "$(dirname "$0")/.."
RUN_DIR=.mnogobase/llama
LOG_DIR=logs
mkdir -p "$RUN_DIR" "$LOG_DIR"

EMBED_MODEL=${LLAMACPP_EMBED_MODEL:-Qwen/Qwen3-Embedding-4B-GGUF:Q4_K_M}
RERANK_MODEL=${LLAMACPP_RERANK_MODEL:-gorse-io/Qwen3-Reranker-4B-GGUF:Q4_K_M}
LLM_MODEL=${LLAMACPP_LLM_MODEL:-unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF:Q4_K_M}
EMBED_ALIAS=${LLAMACPP_EMBED_ALIAS:-qwen3-embedding-4b}
RERANK_ALIAS=${LLAMACPP_RERANK_ALIAS:-qwen3-reranker-4b}
LLM_ALIAS=${LLAMACPP_LLM_ALIAS:-qwen3-30b-a3b-instruct-2507}

args_for() {
  case $1 in
    embed) echo "-hf $EMBED_MODEL --alias $EMBED_ALIAS --embedding --pooling last --ctx-size 8192 --ubatch-size 8192 --port 8001" ;;
    rerank) echo "-hf $RERANK_MODEL --alias $RERANK_ALIAS --rerank --ctx-size 8192 --ubatch-size 8192 --port 8002" ;;
    llm) echo "-hf $LLM_MODEL --alias $LLM_ALIAS --ctx-size ${LLAMACPP_LLM_CTX:-32768} --port 8000" ;;
  esac
}

port_for() {
  case $1 in embed) echo 8001 ;; rerank) echo 8002 ;; llm) echo 8000 ;; esac
}

running() {
  [[ -f "$RUN_DIR/$1.pid" ]] && kill -0 "$(cat "$RUN_DIR/$1.pid")" 2>/dev/null
}

start_one() {
  local name=$1 port
  port=$(port_for "$name")
  if running "$name"; then
    echo "$name: already running (pid $(cat "$RUN_DIR/$name.pid"), port $port)"
    return
  fi
  # shellcheck disable=SC2046  # аргументы намеренно разбиваются по пробелам
  nohup llama-server $(args_for "$name") --n-gpu-layers 999 >"$LOG_DIR/llama-$name.log" 2>&1 &
  echo $! >"$RUN_DIR/$name.pid"
  echo "$name: starting on port $port (log: $LOG_DIR/llama-$name.log; first start downloads the model)"
}

wait_ready() {
  local name=$1 port
  port=$(port_for "$name")
  until curl -fs "http://localhost:$port/health" >/dev/null 2>&1; do
    if ! running "$name"; then
      echo "$name: exited, see $LOG_DIR/llama-$name.log" >&2
      tail -5 "$LOG_DIR/llama-$name.log" >&2
      return 1
    fi
    sleep 2
  done
  echo "$name: ready on http://localhost:$port/v1"
}

stop_one() {
  if running "$1"; then
    kill "$(cat "$RUN_DIR/$1.pid")" && echo "$1: stopped"
  fi
  rm -f "$RUN_DIR/$1.pid"
}

command -v llama-server >/dev/null || { echo "llama-server not found: brew install llama.cpp" >&2; exit 1; }

services=(embed rerank)
[[ ${2:-} == llm ]] && services+=(llm)

case ${1:-start} in
  start)
    for s in "${services[@]}"; do start_one "$s"; done
    for s in "${services[@]}"; do wait_ready "$s"; done
    ;;
  stop)
    for s in embed rerank llm; do stop_one "$s"; done
    ;;
  status)
    for s in embed rerank llm; do
      if running "$s"; then echo "$s: running (pid $(cat "$RUN_DIR/$s.pid"), port $(port_for "$s"))"; else echo "$s: stopped"; fi
    done
    ;;
  *)
    echo "usage: $0 start [llm] | stop | status" >&2
    exit 2
    ;;
esac
