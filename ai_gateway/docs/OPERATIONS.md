# Operations runbook

## Service endpoints

| Endpoint | Purpose |
| --- | --- |
| `http://127.0.0.1:8788/` | Multi-Model AI Gateway web UI |
| `/health` | Gateway liveness |
| `/api/status` | Backend health, routing, access mode, and GitHub status |
| `/metrics` | Prometheus-compatible counters and latency gauges |
| `/v1/models` | OpenAI-compatible model inventory |
| `/v1/chat/completions` | OpenAI-compatible chat API |
| `http://127.0.0.1:8081/v1` | MLX Sentinel GPT-OSS upstream server |

## Persistent services

The US.ANT installation uses one Rust engine launch agent:

- `com.local.us-ant-engine`: `~/Library/LaunchAgents/com.local.us-ant-engine.plist`
- the engine supervises the paper bot, research scheduler, gateway, and MLX Sentinel
- the local plist supplies the verified `US_ANT_PAPER_ACCOUNT_ID`
- logs: `../logs/trading-engine.err` and each worker's local log

Check or restart them:

```sh
launchctl print gui/$(id -u)/com.local.us-ant-engine
./target/release/trading_engine status
```

## Key handling

The gateway reads the NVIDIA credential from:

```text
macOS Keychain service: NVIDIA_API_KEY
macOS Keychain account: your macOS username
```

`NVIDIA_API_KEY` is also accepted for development. Never put a key in `.env`, source files, logs, screenshots, or Git. The API status endpoint deliberately reports only `token_exposure: never returned`.

## MLX recovery

If `/v1/models` works but a completion hangs:

1. inspect `../logs/mlx-sentinel.err` for model-load errors;
2. inspect `../logs/trading-engine.err` for worker restart messages;
3. wait for `/v1/models` to return the configured model;
4. test `/v1/chat/completions` directly on port 8081;
5. retest the gateway on port 8788.

This recovery is safe because MLX is supervised by launchd. A model-list response alone is not proof that generation is ready; always test a completion after a model reload.

## NVIDIA failures

Check `/api/status` and `/metrics`. NVIDIA health is cached briefly to avoid hammering the upstream API. Requests are limited to the verified free-model allowlist. If NVIDIA is unavailable, explicit NVIDIA requests fail clearly; automatic requests may use local MLX when difficulty and health permit.

## Learned router deployment

Train and validate a router outside the gateway, then set `MULTI_MODEL_GATEWAY_ROUTER_MODEL` in the gateway launch environment. `/api/status` reports `learned_router: true` when the file loaded successfully. Remove the variable to return to the built-in difficulty heuristic. Never deploy a model trained on secrets or private customer data into a shared repository.

## Apple failures

The Swift bridge reports on-device and Private Cloud availability separately. On macOS 27, `private_cloud` requests use `PrivateCloudComputeLanguageModel` directly and never silently fall back to the on-device model. The device must be eligible and the Apple Private Cloud Compute entitlement/session must be provisioned; otherwise the request returns a clear error and the gateway can choose another provider.

