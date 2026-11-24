#!/bin/zsh
set -euo pipefail

ROOT="/Users/local.local/Projects/TradingSystem"
APP_ROOT="$ROOT/mlx-sentinel"
PYTHON="$APP_ROOT/.venv/bin/python"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export MLX_SENTINEL_MODEL="${MLX_SENTINEL_MODEL:-mlx-community/Qwen3-0.6B-4bit}"
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
