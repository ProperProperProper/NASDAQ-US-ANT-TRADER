# Paper bot operations

This describes the implementation currently in this repository and the local
macOS services used to run it.

## Runtime components

| Component | Current behavior |
|---|---|
| Moomoo OpenD | Required on `127.0.0.1:11111` |
| Rust engine | `ai_gateway/target/release/trading_engine`, launchd label `com.local.us-ant-engine` |
| Paper bot | `competition_bot.py`, US.ANT, locally configured standard SIMULATE account |
| Initial paper bar | 10 minutes; replaced only by an eligible completed sweep |
| Native app | `/Applications/US.ANT Paper Trader.app` |
| Database | `quant_dashboard.sqlite3` (local research cache) |
| Watchdog | `watch_moomoo_paper_stack.sh` |
| Research scheduler | launchd checker every minute; reruns the same cached session 15 minutes after a successful sweep, at low CPU priority |

The native app controls the paper worker through the Rust engine's local Unix
socket. One launchd job owns the paper bot, research scheduler, AI gateway, and
MLX Sentinel. Moomoo OpenD remains a separate broker application. The Moomoo
and MLX integrations remain Python workers inside the Rust-supervised runtime.
Only the engine's local launch plist supplies `US_ANT_PAPER_ACCOUNT_ID`.

Research runs in a separate low-priority process. Eligible winners are
automatically queued. The paper bot checks its SIMULATE broker position and
pending orders before applying a queued strategy atomically; promotion does
not restart the bot or reset its in-memory execution state. The new interval
and parameters are picked up in the next normal polling cycle.

## OpenD setup

1. Install Moomoo OpenD from the official Moomoo/Futu distribution for macOS.
2. Open OpenD and sign in to the intended Moomoo account.
3. Confirm OpenD's local API server is enabled on `127.0.0.1:11111`.
4. Confirm the port is listening:

   ```sh
   nc -z 127.0.0.1 11111
   ```

5. From this repository, list the accounts visible through OpenD:

   ```sh
   python3 competition_bot.py --list-accounts
   ```

6. Use the standard locally configured SIMULATE account. The bot refuses every
   other account and every non-SIMULATE environment.
7. Open the native app after OpenD is ready, then verify:

   ```sh
   ./watch_moomoo_paper_stack.sh
   ```

The bot uses `OpenQuoteContext` for market data and `OpenSecTradeContext` for
paper execution, both connected to `127.0.0.1:11111`. If OpenD is closed,
logged out, or listening on another port, candle fetches and account operations
fail rather than silently using static data.

## Supported candle intervals

The bot accepts every interval from `1` through `10` minutes. The
default is `10`. Moomoo OpenD is queried only for 1-minute candles; requested
intervals are built locally from complete 09:30–16:00 US-session buckets and
stored with a `local_1m_aggregation` source marker.

## Start and health checks

```sh
launchctl print gui/$(id -u)/com.local.us-ant-engine
./ai_gateway/target/release/trading_engine status
open -a "/Applications/US.ANT Paper Trader.app"
```

While running, `/api/status` should report `active: true`,
`mode: "SIMULATE"`, the locally configured account ID, and the selected
`bar_interval`.

The watchdog checks OpenD on port `11111` and the bot launchd job. It exits
non-zero if a dependency is down or
the bot is not in SIMULATE mode.

## Backtesting

The tester fetches all available pages of 1-minute candles from Moomoo OpenD,
then aggregates the requested interval locally and stores both source and
derived candles in SQLite. A test may score at most 30 calendar days. Longer requests are clamped to 30 days before the requested end, and
future end dates are clamped to today.

A strategy warm-up period is fetched before the scored start date for the
45-bar grid lookback. The response reports `requested_window` and
`warmup_window` separately. Warm-up does not expand the 30-day scored limit.

Use **Stop tester** to cancel a running test. Completed runs are stored in
`backtest_runs`. Results from different intervals are only comparable when the
date window, costs, and strategy settings are the same.

## Safety boundaries

- The locally configured standard SIMULATE account is the only permitted account; never use a competition or live account.
- `--live-paper` means paper execution, not live brokerage execution.
- Keep credentials in macOS Keychain or the process environment; never commit
  them.
- Keep the native app and bot local. Do not publish the bot through an
  unauthenticated reverse proxy.
- HARD RULE: research promotion requires at least two completed US.ANT sessions
  and at least two trades, positive return after costs, and a better result
  than the current paper strategy on the tested data. Eligible winners are
  promoted automatically through the broker-verified flat-position gate.

## Troubleshooting

1. Confirm Moomoo OpenD is installed, logged in, and listening on `127.0.0.1:11111`.
2. Check `/api/health` and `/api/status`.
3. Inspect the launchd job with `launchctl print` and its configured logs.
4. Run `./watch_moomoo_paper_stack.sh`; zero means the local stack passed.
5. If candles are missing, verify the interval has history and retry after
   OpenD completes pagination.
