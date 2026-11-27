# Codex repository instructions

This checkout is the canonical local workspace for the configurable US paper-trading bot.

- Canonical path: `/Users/local.local/Projects/TradingSystem`
- GitHub remote: `https://github.com/ProperProperProper/NASDAQ-US-ANT-TRADER.git`
- Primary branch: `main`
- Active runtime: `competition_bot.py` and the native app `/Applications/Moomoo Paper Trader.app`
- Active symbol/config: `trading_system.json`; symbol-specific strategies live under `strategies/`.
- HARD RULE: execution must remain paper-only on the locally configured standard Moomoo SIMULATE account.
- HARD RULE: the configured US symbol portfolio may trade concurrently. Portfolio changes require removed and added symbols to be flat with no pending orders on the standard SIMULATE account.
- HARD RULE: automatically promote eligible research winners only into the same symbol's paper strategy after the existing evidence gates pass; the bot applies the queued promotion only after verifying the SIMULATE account is flat with no pending order. Do not require manual promotion or route it to a real or competition account.
- Never use a competition or real account, even if it is available.
- Do not run or revive other bot copies from the parent TradingSystem workspace.
- Before reporting the bot as running, verify the process, native app state, SIMULATE
  account, and advancing candle cache.

Keep runtime logs, SQLite databases, credentials, caches, and local launch-agent
files out of Git unless explicitly requested.
