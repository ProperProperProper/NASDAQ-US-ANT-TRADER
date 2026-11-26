#!/bin/zsh
set -euo pipefail

ROOT="/Users/local.local/Projects/TradingSystem"
APP_ROOT="$ROOT/mlx-sentinel"
PYTHON="$APP_ROOT/.venv/bin/python"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
MODEL_REPO="mlx-community/gpt-oss-20b-MXFP4-Q4"
MODEL_SNAPSHOT="/Users/local.local/.cache/huggingface/hub/models--mlx-community--gpt-oss-20b-MXFP4-Q4/snapshots/f356f2747216d7e98fee755df25987459fc19089"
export MLX_SENTINEL_MODEL="${MLX_SENTINEL_MODEL:-$MODEL_REPO}"
if [[ -f "$MODEL_SNAPSHOT/config.json" && -f "$MODEL_SNAPSHOT/model-00001-of-00003.safetensors" && -f "$MODEL_SNAPSHOT/model-00002-of-00003.safetensors" && -f "$MODEL_SNAPSHOT/model-00003-of-00003.safetensors" ]]; then
  export MLX_SENTINEL_MODEL="$MODEL_SNAPSHOT"
fi
export MLX_SENTINEL_MODE="${MLX_SENTINEL_MODE:-enforce}"
export MLX_SENTINEL_SECURITY_URL="${MLX_SENTINEL_SECURITY_URL:-http://127.0.0.1:8788/api/security/kv}"
export MLX_SENTINEL_POLICY_PATH="${MLX_SENTINEL_POLICY_PATH:-$ROOT/security/mlx-sentinel-policy.json}"
export MLX_SENTINEL_ADAPTER_PATH="${MLX_SENTINEL_ADAPTER_PATH:-}"
ADAPTER_ARGS=()
if [[ -n "$MLX_SENTINEL_ADAPTER_PATH" ]]; then
  ADAPTER_ARGS=(--adapter-path "$MLX_SENTINEL_ADAPTER_PATH")
fi

cd "$APP_ROOT"
if command -v sandbox-exec >/dev/null 2>&1; then
  exec sandbox-exec -f "$ROOT/security/mlx-local.sb" "$PYTHON" -m mlx_sentinel.server --host 127.0.0.1 --port 8081 --model "$MLX_SENTINEL_MODEL" "${ADAPTER_ARGS[@]}" --mode "$MLX_SENTINEL_MODE" --security-url "$MLX_SENTINEL_SECURITY_URL"
fi
exec "$PYTHON" -m mlx_sentinel.server --host 127.0.0.1 --port 8081 --model "$MLX_SENTINEL_MODEL" "${ADAPTER_ARGS[@]}" --mode "$MLX_SENTINEL_MODE" --security-url "$MLX_SENTINEL_SECURITY_URL"
