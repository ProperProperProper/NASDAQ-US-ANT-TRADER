# AI engineering controls

This project applies production AI-engineering practices to the bundled local
MLX, NVIDIA, and Apple providers through provider-neutral interfaces.

## Architecture and workflow boundaries

- Deterministic Python research owns candle loading, strategy evaluation, risk,
  and paper configuration promotion.
- AI providers may propose bounded strategy parameters; they cannot place orders,
  change the symbol, select an account, change SIMULATE mode, or edit strategy
  code through the research path.
- The Rust gateway is the provider router and tool boundary. Subagents receive a
  bounded task and a report contract; they cannot call tools or spawn agents.
- Long-running work is split into bounded research runs and stored artifacts,
  rather than an unbounded conversational loop.

## Integration and resilience

- Provider selection is explicit or routed through the gateway's health- and
  load-aware policy.
- NVIDIA models are restricted to the source allowlist of verified free models;
  environment configuration can narrow, not expand, that allowlist.
- Provider timeouts, rate-limit cooldowns, fallback behavior, and health checks
  are surfaced by the gateway. A failed explicit provider request fails clearly.
- Credentials are read from the local Keychain or process environment and are
  never returned by the gateway or MCP bridge.

## Prompt and context controls

- Strategy prompts identify the configured active symbol, the allowed parameter keys, numeric bounds,
  and the required JSON-only response.
- Free-text model output is parsed defensively and reduced to a strict allowlist
  of strategy fields and enum values.
- Repository context is bounded, excludes credential-like files, and instructs
  providers not to expose secrets or claim actions they did not perform.
- Provider output is treated as untrusted input; deterministic Python code owns
  validation, scoring, and promotion.

## Tools and MCP

- The bundled MCP server exposes only bounded gateway-chat and routing-sample
  capabilities to the strategy lab.
- Gateway agent tools are disabled by default in `ops/start_ai_gateway.sh`.
- When explicitly enabled for a reviewed local session, tool names and argument
  types are checked by Rust before execution; writes, shell commands, network
  commands, credential access, destructive commands, and trading actions are
  separately denied or constrained.
- Tool descriptions state the execution boundary and the returned data is kept
  minimal for the model's next decision.

## Evaluation and release gates

- Python unit tests cover causal grid features, sell behavior, and the hard
  research-promotion gate.
- Rust tests cover security telemetry validation and denial of trading actions;
  Rust formatting and release tests run before release commits.
- Research records parameters, costs, return, drawdown, trade count, and the
  complete run artifact. Results with fewer than two completed same-symbol sessions,
  fewer than two trades, or weak evidence are held and cannot be promoted.
- A promotion result is research evidence, not a performance guarantee. The
  strategy result must remain traceable to its data window and run ID.

## Security and privacy

- The gateway binds to loopback and launches in readonly, security-enforced,
  sandboxed mode by default.
- Prompt-injection, high-risk command, credential-access, file-write, and
  trading-action checks are deterministic and outside model discretion.
- Raw prompts and KV tensors are not persisted by the security telemetry path.
- Secrets, runtime logs, SQLite data, local launch agents, and account IDs stay
  outside the public repository.

## Operational rule

If an AI provider is unavailable, malformed, untrusted, or outside its allowed
scope, the deterministic active-symbol paper-trading code continues with its validated
baseline or holds the research result. AI output is never an execution authority.
