#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT/.venv-multi-model-gateway"
if [[ ! -x "$VENV/bin/mlx_lm.server" ]]; then
  VENV="$ROOT/mlx-sentinel/.venv"
fi
MODEL="${MULTI_MODEL_GATEWAY_MLX_MODEL:-mlx-community/gpt-oss-20b-MXFP4-Q4}"

cd "$ROOT"
if command -v sandbox-exec >/dev/null 2>&1; then
  exec sandbox-exec -f "$ROOT/security/mlx-local.sb" "$VENV/bin/mlx_lm.server" \
    --model "$MODEL" \
    --host 127.0.0.1 \
    --port 8080 \
    --chat-template-args '{"enable_thinking":false}' \
    --log-level WARNING
fi
exec "$VENV/bin/mlx_lm.server" \
  --model "$MODEL" \
  --host 127.0.0.1 \
  --port 8080 \
  --chat-template-args '{"enable_thinking":false}' \
  --log-level WARNING
