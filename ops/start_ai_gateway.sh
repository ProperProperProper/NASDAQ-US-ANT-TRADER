#!/bin/zsh
set -euo pipefail

APP_ROOT="$(cd "$(dirname "$0")/../ai_gateway" && pwd)"
ROOT="$(cd "$APP_ROOT/.." && pwd)"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export NVIDIA_KEYCHAIN_SERVICE="${NVIDIA_KEYCHAIN_SERVICE:-NVIDIA_API_KEY}"
export NVIDIA_KEYCHAIN_ACCOUNT="${NVIDIA_KEYCHAIN_ACCOUNT:-local.local}"
export NVIDIA_MODEL="${NVIDIA_MODEL:-nvidia/nemotron-3.5-lightning-30b-a3b}"
export NVIDIA_FAST_MODEL="${NVIDIA_FAST_MODEL:-nvidia/nemotron-3.5-bolt-e4b}"
export NVIDIA_HARD_MODEL="${NVIDIA_HARD_MODEL:-deepseek-ai/deepseek-v4.1-flash}"
export NVIDIA_FALLBACK_MODEL="${NVIDIA_FALLBACK_MODEL:-nvidia/nemotron-3.5-bolt-e4b}"
export NVIDIA_REQUEST_TIMEOUT="${NVIDIA_REQUEST_TIMEOUT:-15}"
export MLX_REQUEST_TIMEOUT="${MLX_REQUEST_TIMEOUT:-10}"
export MULTI_MODEL_GATEWAY_TOOL_TIMEOUT="${MULTI_MODEL_GATEWAY_TOOL_TIMEOUT:-120}"
# MLX is the primary gateway backend; NVIDIA is used only on local failure.
export AI_PROVIDER="${AI_PROVIDER:-auto}"
export AI_LOCAL_MAX_DIFFICULTY="${AI_LOCAL_MAX_DIFFICULTY:-255}"
export MLX_MODEL="${MLX_MODEL:-mlx-community/Qwen3-0.6B-4bit}"
# Use MLX Sentinel's bounded OpenAI-compatible server. The stock mlx_lm.server
# currently spins at ~100% CPU while idle on this Mac.
export MLX_BASE_URL="${MLX_BASE_URL:-http://127.0.0.1:8081/v1}"
# Safe-by-default: chat has no local write/shell tools. Enable capabilities only
# for a reviewed, temporary session with an explicit environment override.
export MULTI_MODEL_GATEWAY_ACCESS_MODE="${MULTI_MODEL_GATEWAY_ACCESS_MODE:-readonly}"
export MULTI_MODEL_GATEWAY_ENABLE_AGENT_TOOLS="${MULTI_MODEL_GATEWAY_ENABLE_AGENT_TOOLS:-0}"
export MULTI_MODEL_GATEWAY_ENABLE_FILE_WRITES="${MULTI_MODEL_GATEWAY_ENABLE_FILE_WRITES:-0}"
export MULTI_MODEL_GATEWAY_ENABLE_COMMANDS="${MULTI_MODEL_GATEWAY_ENABLE_COMMANDS:-0}"
export MULTI_MODEL_GATEWAY_SECURITY_MODE="${MULTI_MODEL_GATEWAY_SECURITY_MODE:-enforce}"
export MULTI_MODEL_GATEWAY_SECURITY_AGENT_MODE="${MULTI_MODEL_GATEWAY_SECURITY_AGENT_MODE:-enforce}"
export MULTI_MODEL_GATEWAY_REPO_ROOT="$APP_ROOT"
export MULTI_MODEL_GATEWAY_APPLE_AI_BIN="${MULTI_MODEL_GATEWAY_APPLE_AI_BIN:-$APP_ROOT/apple-ai-bridge/.build/release/multi-model-apple-ai}"
# Prefer the available on-device Foundation Model. Private Cloud requires an
# Apple-managed entitlement that is not present on every developer account.
export MULTI_MODEL_GATEWAY_APPLE_AI_MODE="${MULTI_MODEL_GATEWAY_APPLE_AI_MODE:-on_device}"

cd "$APP_ROOT"
if [[ "${MULTI_MODEL_GATEWAY_SANDBOX:-1}" == "1" ]] && command -v sandbox-exec >/dev/null 2>&1; then
  exec sandbox-exec -f "$ROOT/security/gateway-agent.sb" "$APP_ROOT/target/release/multi-model-ai-gateway" serve
fi
exec "$APP_ROOT/target/release/multi-model-ai-gateway" serve
