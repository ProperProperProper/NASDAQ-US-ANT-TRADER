# Codex repository instructions

This checkout is the canonical local workspace for the US.ANT paper-trading bot.

- Canonical path: `/Users/local.local/Projects/TradingSystem`
- GitHub remote: `https://github.com/ProperProperProper/NASDAQ-US-ANT-TRADER.git`
- Primary branch: `main`
- Active runtime: `competition_bot.py` and the native app `/Applications/US.ANT Paper Trader.app`
- Active strategy/config: `strategy_57pct.py` and `paper_bot_live_strategy.json` for US.ANT
- Execution must remain paper-only on Moomoo SIMULATE account `COMPETITION_ACCOUNT_ID`.
- Do not run or revive other bot copies from the parent TradingSystem workspace.
- Before reporting the bot as running, verify the process, native app state, SIMULATE
  account, and advancing candle cache.

Keep runtime logs, SQLite databases, credentials, caches, and local launch-agent
files out of Git unless explicitly requested.
