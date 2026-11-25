#!/usr/bin/env python3
"""Moomoo paper-competition bot for the US.ANT causal price-grid strategy.

The bot is paper-only by construction: every order uses TrdEnv.SIMULATE.
Order submission additionally requires --live-paper. Without that flag the
bot prints signals and intended orders but never submits anything.

Strategy:
  - 45 completed bars for the causal price-grid signal
  - long-only, one position per symbol
  - paper-only risk and execution controls
  - 95% of available cash per entry

Moomoo OpenD must be running on 127.0.0.1:11111 and logged in. This script
does not contain credentials or attempt to unlock a live account.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import signal
import socket
import time
from datetime import datetime, timedelta, time as dt_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from moomoo import (
    KLType,
    OpenQuoteContext,
    OpenSecTradeContext,
    OrderType,
    RET_OK,
    TrdEnv,
    TrdMarket,
    TrdSide,
)

from quant_storage import save_candles, save_execution_event
from minute_bars import MINUTE_INTERVALS, aggregate_us_session
from quant_api import LIVE_STRATEGY_PATH, audit_api, risk_api, strategy_api
from strategy_57pct import BASELINE


POSITION_FRACTION = 0.95
MAX_OPEN_POSITIONS = 1
POLL_SECONDS = 30
MAX_GRID_LOOKBACK = 240
MIN_BARS = MAX_GRID_LOOKBACK + 5
OPEN_D_SOCKET_TIMEOUT_SECONDS = 20
DEFAULT_BAR_INTERVAL = 10
STANDARD_PAPER_ACCOUNT_ID = int(os.environ.get("US_ANT_PAPER_ACCOUNT_ID", "0"))
ACTIVE_SYMBOL = "US.ANT"
BAR_INTERVAL_TYPES = {minutes: KLType.K_1M for minutes in MINUTE_INTERVALS}
ET = ZoneInfo("America/New_York")
SESSION_EXIT_TIME = dt_time(15, 30)
SESSION_CLOSE_TIME = dt_time(16, 0)
STATE_PATH = Path(__file__).with_name("competition_bot_state.json")
LOG_PATH = Path(__file__).with_name("competition_bot.log")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()],
    )
    socket.setdefaulttimeout(OPEN_D_SOCKET_TIMEOUT_SECONDS)


def scalar(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def count_open_positions(data: pd.DataFrame) -> int:
    """Count positive holdings returned by the SIMULATE position query."""
    if data is None or data.empty:
        return 0
    count = 0
    for _, row in data.iterrows():
        if scalar(row.get("qty", row.get("position", 0))) > 0:
            count += 1
    return count


def code(symbol: str) -> str:
    symbol = symbol.upper().strip()
    return symbol if "." in symbol else f"US.{symbol}"


class CompetitionBot:
    def __init__(self, symbols: list[str], acc_index: int, live_paper: bool, interval: int, acc_id: int | None = None, bar_interval: int = DEFAULT_BAR_INTERVAL) -> None:
        if bar_interval not in BAR_INTERVAL_TYPES:
            raise ValueError(f"unsupported bar interval: {bar_interval}")
        if not STANDARD_PAPER_ACCOUNT_ID or acc_id != STANDARD_PAPER_ACCOUNT_ID:
            raise ValueError("only the locally configured standard SIMULATE account is permitted")
        normalized_symbols = [code(s) for s in symbols]
        if normalized_symbols != [ACTIVE_SYMBOL]:
            raise ValueError(f"only {ACTIVE_SYMBOL} is permitted by the active paper bot")
        self.symbols = normalized_symbols
        self.acc_index = acc_index
        self.acc_id = acc_id
        self.live_paper = live_paper
        self.interval = interval
        self.bar_interval = bar_interval
        self.running = True
        self.last_error: str | None = None
        self.state = self._load_state()
        self.peak_price = scalar(self.state.get("peak_price"), 0.0)
        self.break_even_armed = bool(self.state.get("break_even_armed", False))
        self.profit_lock_armed = bool(self.state.get("profit_lock_armed", False))
        self.quote = OpenQuoteContext(host="127.0.0.1", port=11111)
        self.trade = OpenSecTradeContext(
            filter_trdmarket=TrdMarket.US,
            host="127.0.0.1",
            port=11111,
        )

    def _load_state(self) -> dict[str, Any]:
        if STATE_PATH.exists():
            state = json.loads(STATE_PATH.read_text())
        else:
            state = {"last_bar": {}, "last_source_bar": {}, "entries": {}}
        state.setdefault("last_source_bar", {})
        state.setdefault("pending", {})
        return state

    def _save_state(self) -> None:
        self.state["peak_price"] = self.peak_price
        self.state["break_even_armed"] = self.break_even_armed
        self.state["profit_lock_armed"] = self.profit_lock_armed
        self.state["bar_interval"] = self.bar_interval
        STATE_PATH.write_text(json.dumps(self.state, indent=2) + "\n")

    def _strategy_config(self, symbol: str) -> tuple[int, dict[str, Any]]:
        """Reload the published paper interval and parameters on every cycle."""
        try:
            payload = json.loads(LIVE_STRATEGY_PATH.read_text())
            interval = int(payload.get("interval"))
            if (str(payload.get("symbol", "")).upper() == symbol
                    and interval in BAR_INTERVAL_TYPES
                    and payload.get("mode") == "SIMULATE"
                    and isinstance(payload.get("params"), dict)):
                return interval, {**BASELINE, **payload["params"]}
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
        return self.bar_interval, dict(BASELINE)

    def verify_sim_account(self) -> None:
        """Fail closed when the configured paper account is unavailable."""
        if self.acc_id != STANDARD_PAPER_ACCOUNT_ID:
            raise RuntimeError("only the locally configured standard SIMULATE account is permitted")
        ret, data = self.trade.get_acc_list()
        if ret != RET_OK:
            raise RuntimeError(f"SIMULATE account query failed: {data}")
        if self.acc_id is None:
            return
        rows = data[data["acc_id"].astype(str) == str(self.acc_id)] if "acc_id" in data.columns else data.iloc[0:0]
        if rows.empty:
            available = data[[c for c in ("acc_id", "trd_env") if c in data.columns]].to_dict("records")
            raise RuntimeError(f"configured SIMULATE account {self.acc_id} does not exist; available accounts: {available}")
        env = str(rows.iloc[0].get("trd_env", "")).upper()
        if env != "SIMULATE":
            raise RuntimeError(f"configured account {self.acc_id} is {env}, not SIMULATE")

    def close(self) -> None:
        self.quote.close()
        self.trade.close()

    def stop(self, *_: Any) -> None:
        self.running = False

    def account_funds(self) -> float:
        kwargs = {"trd_env": TrdEnv.SIMULATE, "acc_id": self.acc_id} if self.acc_id else {"trd_env": TrdEnv.SIMULATE, "acc_index": self.acc_index}
        ret, data = self.trade.accinfo_query(**kwargs)
        if ret != RET_OK:
            raise RuntimeError(f"account query failed: {data}")
        # SIMULATE margin accounts can report available_funds as the literal
        # string N/A even when cash and buying power are valid. Prefer cash so
        # sizing stays conservative, then fall back to the other numeric
        # buying-power fields.
        for field in ("cash", "available_funds", "us_cash", "power", "total_assets"):
            if field in data.columns:
                value = scalar(data.iloc[0][field])
                if value > 0:
                    return value
        raise RuntimeError(f"no usable cash field in account response: {list(data.columns)}")

    def position(self, symbol: str) -> tuple[float, float]:
        kwargs = {"trd_env": TrdEnv.SIMULATE, "acc_id": self.acc_id} if self.acc_id else {"trd_env": TrdEnv.SIMULATE, "acc_index": self.acc_index}
        ret, data = self.trade.position_list_query(**kwargs)
        if ret != RET_OK:
            raise RuntimeError(f"position query failed: {data}")
        if data.empty or "code" not in data.columns:
            return 0.0, 0.0
        rows = data[data["code"].astype(str) == symbol]
        if rows.empty:
            return 0.0, 0.0
        row = rows.iloc[0]
        qty = scalar(row.get("qty", row.get("position", 0)))
        avg = scalar(row.get("cost_price", row.get("average_cost", 0)))
        return qty, avg

    def open_position_count(self) -> int:
        kwargs = {"trd_env": TrdEnv.SIMULATE, "acc_id": self.acc_id} if self.acc_id else {"trd_env": TrdEnv.SIMULATE, "acc_index": self.acc_index}
        ret, data = self.trade.position_list_query(**kwargs)
        if ret != RET_OK:
            raise RuntimeError(f"position query failed: {data}")
        return count_open_positions(data)

    def pending_order_side(self, symbol: str) -> str | None:
        """Return a live broker order side so restarts cannot duplicate orders."""
        kwargs = {"trd_env": TrdEnv.SIMULATE, "acc_id": self.acc_id} if self.acc_id else {"trd_env": TrdEnv.SIMULATE, "acc_index": self.acc_index}
        ret, data = self.trade.order_list_query(**kwargs)
        if ret != RET_OK:
            raise RuntimeError(f"order query failed: {data}")
        if data is None or data.empty or "code" not in data.columns:
            return None
        terminal = {"FILLED_ALL", "CANCELLED_ALL", "CANCELLED_PART", "FAILED", "DELETED", "CANCELLED"}
        rows = data[data["code"].astype(str) == symbol]
        rows = rows[~rows["order_status"].astype(str).str.upper().isin(terminal)] if "order_status" in rows.columns else rows.iloc[0:0]
        if rows.empty or "trd_side" not in rows.columns:
            return None
        side = str(rows.sort_values("create_time").iloc[-1]["trd_side"]).upper()
        return side if side in {"BUY", "SELL"} else None

    @staticmethod
    def session_exit_due() -> bool:
        now = datetime.now(ET).time()
        return SESSION_EXIT_TIME <= now < SESSION_CLOSE_TIME

    def market_price(self, symbol: str) -> float:
        ret, data = self.quote.get_market_snapshot([symbol])
        if ret != RET_OK or data.empty:
            raise RuntimeError(f"snapshot failed for {symbol}: {data}")
        for field in ("last_price", "cur_price"):
            if field in data.columns:
                value = scalar(data.iloc[0][field])
                if value > 0:
                    return value
        raise RuntimeError(f"snapshot returned no positive price for {symbol}: {list(data.columns)}")

    def market_bid(self, symbol: str) -> float:
        ret, data = self.quote.get_market_snapshot([symbol])
        if ret != RET_OK or data.empty:
            raise RuntimeError(f"snapshot failed for {symbol}: {data}")
        bid = scalar(data.iloc[0].get("bid_price"))
        return bid if bid > 0 else self.market_price(symbol)

    def record_position_mark(self, symbol: str, qty: float, avg: float) -> float | None:
        if qty <= 0 or avg <= 0:
            self.state.pop("position_mark", None)
            self._save_state()
            return None
        mark = self.market_bid(symbol)
        self.state["position_mark"] = {
            "symbol": symbol,
            "qty": int(qty),
            "avg_price": avg,
            "mark_price": mark,
            "gross_unrealized_pnl": round((mark - avg) * qty, 2),
            "mark_time": datetime.now(ET).isoformat(),
        }
        self._save_state()
        return mark

    def risk_exit_reason(self, price: float, qty: float, avg: float, params: dict[str, Any]) -> str | None:
        """Evaluate executable intrabar risk exits without consuming a signal bar."""
        if qty <= 0 or avg <= 0 or price <= 0:
            return None
        if self.peak_price <= 0:
            self.peak_price = max(avg, price)
        self.peak_price = max(self.peak_price, price)
        take_profit_pct = max(0.0, scalar(params.get("take_profit_pct"), 0.0274))
        stop_loss_pct = max(0.0, scalar(params.get("stop_loss_pct"), 0.1324))
        trailing_activation_pct = max(0.0, scalar(params.get("trailing_activation_pct"), 0.0))
        trailing_stop_pct = max(0.0, scalar(params.get("trailing_stop_pct"), 0.0))
        if take_profit_pct > 0 and price >= avg * (1.0 + take_profit_pct):
            return "TAKE_PROFIT"
        if stop_loss_pct > 0 and price <= avg * (1.0 - stop_loss_pct):
            return "STOP_LOSS"
        trailing_armed = (
            trailing_activation_pct > 0
            and trailing_stop_pct > 0
            and self.peak_price >= avg * (1.0 + trailing_activation_pct)
        )
        if trailing_armed and price <= self.peak_price * (1.0 - trailing_stop_pct):
            return "TRAILING_STOP"
        return None

    def candles(self, symbol: str) -> pd.DataFrame:
        now = datetime.now()
        start = (now - timedelta(days=14)).strftime("%Y-%m-%d")
        end = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        frames = []
        page_key = None
        while True:
            ret, data, page_key = self.quote.request_history_kline(
                symbol,
                start=start,
                end=end,
                ktype=KLType.K_1M,
                max_count=1000,
                page_req_key=page_key,
                extended_time=False,
            )
            if ret != RET_OK:
                raise RuntimeError(f"candle query failed for {symbol}: {data}")
            if data is not None and not data.empty:
                frames.append(data.copy())
            if page_key is None:
                break
        if not frames:
            raise RuntimeError(f"no 1-minute candles returned for {symbol}")
        minute_frame = pd.concat(frames, ignore_index=True)
        minute_frame["time_key"] = pd.to_datetime(minute_frame["time_key"])
        minute_frame = minute_frame.sort_values("time_key").drop_duplicates("time_key").set_index("time_key")
        save_candles(minute_frame, symbol, 1)
        bars = aggregate_us_session(minute_frame, self.bar_interval, include_incomplete=True)
        if len(bars) > MIN_BARS + 20:
            bars = bars.tail(MIN_BARS + 20)
        save_candles(bars, symbol, self.bar_interval, source="local_1m_aggregation")
        return bars.reset_index(names="time_key")

    def submit(self, symbol: str, side: Any, qty: int, reason: str) -> None:
        logging.info("%s %s qty=%d mode=%s", reason, symbol, qty, "LIVE_PAPER" if self.live_paper else "DRY_RUN")
        save_execution_event("INTENDED_ORDER", symbol, {"side": str(side), "qty": qty, "reason": reason, "paper": True})
        if not self.live_paper:
            return
        order_kwargs = {"trd_env": TrdEnv.SIMULATE, "acc_id": self.acc_id} if self.acc_id else {"trd_env": TrdEnv.SIMULATE, "acc_index": self.acc_index}
        ret, data = self.trade.place_order(
            price=0,
            qty=qty,
            code=symbol,
            trd_side=side,
            order_type=OrderType.MARKET,
            **order_kwargs,
        )
        if ret != RET_OK:
            save_execution_event("ORDER_ERROR", symbol, {"side": str(side), "qty": qty, "reason": reason, "error": str(data), "paper": True})
            raise RuntimeError(f"paper order failed for {symbol}: {data}")
        logging.info("order accepted: %s", data.to_dict("records"))
        save_execution_event("ORDER_ACCEPTED", symbol, {"side": str(side), "qty": qty, "reason": reason, "response": data.to_dict("records"), "paper": True})

    def tick(self) -> None:
        for symbol in self.symbols:
            try:
                logging.info("poll begin symbol=%s", symbol)
                logging.info("requesting candles symbol=%s", symbol)
                strategy_interval, params = self._strategy_config(symbol)
                if strategy_interval != self.bar_interval:
                    logging.info("adopting promoted paper interval symbol=%s old=%dm new=%dm", symbol, self.bar_interval, strategy_interval)
                    self.bar_interval = strategy_interval
                    save_execution_event("STRATEGY_INTERVAL_ADOPTED", symbol, {"interval": strategy_interval, "params": params, "paper": True})
                if self.state.get("strategy_interval") != strategy_interval or self.state.get("strategy_params") != params:
                    self.state["strategy_interval"] = strategy_interval
                    self.state["strategy_params"] = params
                    self._save_state()
                candles = self.candles(symbol)
                if candles.empty or "time_key" not in candles.columns:
                    raise RuntimeError(f"no usable candles returned for {symbol}")
                source_bar = str(candles["time_key"].iloc[-1])
                # The strategy consumes the last completed bar.  Recomputing
                # features on every 30-second poll while the source candle is
                # unchanged is pure overhead and can create unnecessary SQLite
                # and provider pressure.  Pending orders are still checked on
                # every poll so paper fills are not held until the next bar.
                if self.state["last_source_bar"].get(symbol) == source_bar:
                    pending = self.pending_order_side(symbol)
                    if pending != self.state["pending"].get(symbol):
                        if pending:
                            self.state["pending"][symbol] = pending
                        else:
                            self.state["pending"].pop(symbol, None)
                        self._save_state()
                    if self.session_exit_due() and not pending:
                        qty, avg = self.position(symbol)
                        if qty > 0:
                            self.submit(symbol, TrdSide.SELL, int(qty), "SESSION_EXIT_30M_BEFORE_CLOSE")
                            self.state["pending"][symbol] = "SELL"
                            self.peak_price = 0.0
                            self._save_state()
                            continue
                    if pending:
                        qty, _ = self.position(symbol)
                        filled = (pending == "BUY" and qty > 0) or (pending == "SELL" and qty <= 0)
                        if filled:
                            self.state["pending"].pop(symbol, None)
                            self._save_state()
                    qty, avg = self.position(symbol)
                    mark = self.record_position_mark(symbol, qty, avg)
                    risk_reason = self.risk_exit_reason(mark or 0.0, qty, avg, params)
                    if risk_reason and not pending:
                        self.submit(symbol, TrdSide.SELL, int(qty), risk_reason)
                        self.state["pending"][symbol] = "SELL"
                        self.peak_price = 0.0
                        self.break_even_armed = False
                        self.profit_lock_armed = False
                        self._save_state()
                        continue
                    logging.info("no new source candle symbol=%s bar=%s", symbol, source_bar)
                    continue
                signal_data = strategy_api.signal(candles, params)
                if self.state["last_bar"].get(symbol) == signal_data["bar"]:
                    logging.info("no new completed bar symbol=%s bar=%s", symbol, signal_data["bar"])
                    self.state["last_source_bar"][symbol] = source_bar
                    self._save_state()
                    continue
                logging.info("querying position symbol=%s", symbol)
                qty, avg = self.position(symbol)
                logging.info("querying snapshot symbol=%s", symbol)
                price = self.market_price(symbol)
                self.record_position_mark(symbol, qty, avg)
                exit_signal = bool(signal_data.get("exit_signal", signal_data.get("trend_exit", False)))
                logging.info("%s bar=%s price=%.4f position=%.4f signal=%s exit=%s", symbol, signal_data["bar"], price, qty, signal_data["long_signal"], exit_signal)

                pending = self.pending_order_side(symbol)
                if pending != self.state["pending"].get(symbol):
                    if pending:
                        self.state["pending"][symbol] = pending
                    else:
                        self.state["pending"].pop(symbol, None)
                    self._save_state()
                if pending:
                    filled = (pending == "BUY" and qty > 0) or (pending == "SELL" and qty <= 0)
                    if filled:
                        self.state["pending"].pop(symbol, None)
                    else:
                        self.state["last_bar"][symbol] = signal_data["bar"]
                        self.state["last_source_bar"][symbol] = source_bar
                        self._save_state()
                        continue

                if qty > 0:
                    if self.peak_price <= 0:
                        self.peak_price = max(avg, price)
                    self.peak_price = max(self.peak_price, price)
                    risk_reason = self.risk_exit_reason(price, qty, avg, params)
                    session_exit = self.session_exit_due() or bool(signal_data.get("session_exit", False))
                    if risk_reason or session_exit or exit_signal:
                        reason = risk_reason or ("SESSION_EXIT_30M_BEFORE_CLOSE" if session_exit else "GRID_EXIT")
                        self.submit(symbol, TrdSide.SELL, int(qty), reason)
                        self.state["pending"][symbol] = "SELL"
                        self.peak_price = 0.0
                        self.break_even_armed = False
                        self.profit_lock_armed = False
                    self.state["last_bar"][symbol] = signal_data["bar"]
                    self.state["last_source_bar"][symbol] = source_bar
                    self._save_state()
                    continue

                if signal_data["long_signal"] and signal_data["entry_allowed"]:
                    open_positions = self.open_position_count()
                    if open_positions >= MAX_OPEN_POSITIONS:
                        logging.info("entry blocked symbol=%s reason=MAX_OPEN_POSITIONS open_positions=%d limit=%d", symbol, open_positions, MAX_OPEN_POSITIONS)
                        audit_api.event("ENTRY_BLOCKED", symbol, {"reason": "MAX_OPEN_POSITIONS", "open_positions": open_positions, "limit": MAX_OPEN_POSITIONS, "price": price, "signal": signal_data, "paper": True})
                    else:
                        cash = self.account_funds()
                        decision = risk_api.entry_decision(cash, price, POSITION_FRACTION)
                        audit_api.event("RISK_DECISION", symbol, {**decision.__dict__, "cash": cash, "price": price, "open_positions": open_positions, "max_open_positions": MAX_OPEN_POSITIONS, "signal": signal_data})
                        if decision.allowed:
                            self.submit(symbol, TrdSide.BUY, decision.quantity, "ENTRY")
                            self.state["pending"][symbol] = "BUY"
                            self.peak_price = price
                            self.break_even_armed = False
                            self.profit_lock_armed = False
                    self.state["last_bar"][symbol] = signal_data["bar"]
                    self.state["last_source_bar"][symbol] = source_bar
                    self._save_state()
                    continue
                # Mark the bar only after all queries and any order submission
                # have completed. A failed cycle can therefore be retried.
                self.state["last_bar"][symbol] = signal_data["bar"]
                self.state["last_source_bar"][symbol] = source_bar
                self._save_state()
            except Exception as exc:
                self.last_error = repr(exc)
                logging.exception("symbol cycle failed: %s", symbol)
                save_execution_event("CYCLE_ERROR", symbol, {"paper": True, "error": repr(exc)})

    def run(self) -> None:
        self.verify_sim_account()
        logging.info("starting %s symbols=%s account_id=%s bar_interval=%dm poll_seconds=%d", "LIVE_PAPER" if self.live_paper else "DRY_RUN", self.symbols, self.acc_id, self.bar_interval, self.interval)
        while self.running:
            self.tick()
            for _ in range(self.interval):
                if not self.running:
                    break
                time.sleep(1)
        logging.info("stopped")


def list_accounts() -> int:
    ctx = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host="127.0.0.1", port=11111)
    try:
        ret, data = ctx.get_acc_list()
        if ret != RET_OK:
            print(data)
            return 1
        print(data.to_string(index=False))
        return 0
    finally:
        ctx.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="*", help=f"active symbol only: {ACTIVE_SYMBOL} (or ANT)")
    parser.add_argument("--acc-index", type=int, default=0, help="SIMULATE US account index from --list-accounts")
    parser.add_argument("--acc-id", type=int, default=STANDARD_PAPER_ACCOUNT_ID, help="SIMULATE account ID (must match local configuration)")
    parser.add_argument("--interval", type=int, default=POLL_SECONDS)
    parser.add_argument("--bar-interval", type=int, default=DEFAULT_BAR_INTERVAL, choices=sorted(BAR_INTERVAL_TYPES), help="Moomoo candle interval in minutes")
    parser.add_argument("--live-paper", action="store_true", help="Actually submit TrdEnv.SIMULATE orders")
    parser.add_argument("--list-accounts", action="store_true")
    args = parser.parse_args()
    setup_logging()
    if args.list_accounts:
        return list_accounts()
    if not args.symbols:
        parser.error("provide at least one symbol, or use --list-accounts")
    bot = CompetitionBot(args.symbols, args.acc_index, args.live_paper, args.interval, acc_id=args.acc_id, bar_interval=args.bar_interval)
    signal.signal(signal.SIGINT, bot.stop)
    signal.signal(signal.SIGTERM, bot.stop)
    try:
        bot.run()
    finally:
        bot.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
