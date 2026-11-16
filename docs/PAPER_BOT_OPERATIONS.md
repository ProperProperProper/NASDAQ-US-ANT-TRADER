# Paper bot operations

This describes the implementation currently in this repository and the local
macOS services used to run it.

## Runtime components

| Component | Current behavior |
|---|---|
| Moomoo OpenD | Required on `127.0.0.1:11111` |
| Paper bot | `competition_bot.py`, US.APP, SIMULATE account `COMPETITION_ACCOUNT_ID` |
| Default live-paper bar | 10 minutes |
| Dashboard | `quant_dashboard.py` on `127.0.0.1:8780` |
| Database | `quant_dashboard.sqlite3` |
| Watchdog | `watch_moomoo_paper_stack.sh` |

The dashboard is loopback-only and is not exposed on the LAN. Moomoo OpenD is a
separate local service.

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

6. Use the SIMULATE account. The installed service is pinned to account
   `COMPETITION_ACCOUNT_ID`; the bot refuses non-SIMULATE trading environments.
7. Start or restart the local dashboard after OpenD is ready, then verify:

   ```sh
   curl -s http://127.0.0.1:8780/api/health
   curl -s http://127.0.0.1:8780/api/status
   ```

The bot uses `OpenQuoteContext` for market data and `OpenSecTradeContext` for
paper execution, both connected to `127.0.0.1:11111`. If OpenD is closed,
logged out, or listening on another port, candle fetches and account operations
fail rather than silently using static data.

## Supported candle intervals

The bot and dashboard accept `1`, `3`, `5`, `10`, `15`, `30`, `60`, `120`,
`180`, `240`, and `1440` minutes (`1440` is daily). The default is `10`.
The selected interval is passed to Moomoo OpenD and stored with fetched candles;
it is not only a display setting.

## Start and health checks

```sh
launchctl print gui/$(id -u)/com.local.nasdaq-paper-bot
curl -s http://127.0.0.1:8780/api/health
curl -s http://127.0.0.1:8780/api/status
open http://127.0.0.1:8780
```

While running, `/api/status` should report `active: true`,
`mode: "SIMULATE"`, account `COMPETITION_ACCOUNT_ID`, and the selected `bar_interval`.

The watchdog checks OpenD on port `11111`, the bot launchd job, and the
dashboard health/status endpoints. It exits non-zero if a dependency is down or
the bot is not in SIMULATE mode.

## Backtesting

The tester fetches all available pages for the selected interval from Moomoo
OpenD and stores the candles in SQLite. A test may score at most 30 calendar
days. Longer requests are clamped to 30 days before the requested end, and
future end dates are clamped to today.

A strategy warm-up period is fetched before the scored start date for the
45-bar grid lookback. The response reports `requested_window` and
`warmup_window` separately. Warm-up does not expand the 30-day scored limit.

Use **Stop tester** to cancel a running test. Completed runs are stored in
`backtest_runs`. Results from different intervals are only comparable when the
date window, costs, and strategy settings are the same.

## Safety boundaries

- Account `COMPETITION_ACCOUNT_ID` is SIMULATE-only; do not replace it with a live account ID.
- `--live-paper` means paper execution, not live brokerage execution.
- Keep credentials in macOS Keychain or the process environment; never commit
  them.
- Keep the bot and dashboard loopback-only. Do not bind them to `0.0.0.0` or
  publish them through an unauthenticated reverse proxy.

## Troubleshooting

1. Confirm Moomoo OpenD is installed, logged in, and listening on `127.0.0.1:11111`.
2. Check `/api/health` and `/api/status`.
3. Inspect the launchd job with `launchctl print` and its configured logs.
4. Run `./watch_moomoo_paper_stack.sh`; zero means the local stack passed.
5. If candles are missing, verify the interval has history and retry after
   OpenD completes pagination.
