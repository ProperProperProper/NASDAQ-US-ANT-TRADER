#!/usr/bin/env python3
"""Compact read-only active-symbol monitor; exits shortly after US close."""
from __future__ import annotations

import datetime as dt
import json
import os
import plistlib
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from moomoo import OpenQuoteContext, OpenSecTradeContext, RET_OK, TrdEnv, TrdMarket

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from us_market_calendar import session_phase
from trading_config import active_symbol, state_path
ET = ZoneInfo("America/New_York")
ACCOUNT_PLIST = Path.home() / "Library/LaunchAgents/com.local.trading-engine.plist"
SYMBOL = active_symbol()
TERMINAL = {"FILLED_ALL", "CANCELLED_ALL", "CANCELLED_PART", "FAILED", "DELETED", "CANCELLED"}


def account_id() -> int:
    value = os.environ.get("PAPER_ACCOUNT_ID")
    if value:
        return int(value)
    with ACCOUNT_PLIST.open("rb") as handle:
        configured = plistlib.load(handle).get("EnvironmentVariables", {})
    return int(configured["PAPER_ACCOUNT_ID"])


def process_count(fragment: str) -> int:
    result = subprocess.run(["/bin/ps", "-axo", "comm=,args="], capture_output=True, text=True, check=False)
    count = 0
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2:
            continue
        _, arguments = fields
        program = arguments.split(None, 1)[0] if arguments else ""
        if Path(program).name.lower() in {"python", "python3"} and fragment in arguments and "session_monitor.py" not in arguments:
            count += 1
    return count


def check() -> dict:
    acct = account_id()
    trade = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host="127.0.0.1", port=11111)
    quote = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        ret, accounts = trade.get_acc_list()
        row = accounts[accounts.acc_id.astype(str) == str(acct)] if ret == RET_OK else accounts.iloc[0:0]
        env = str(row.iloc[0].trd_env) if not row.empty else "MISSING"
        ret, positions = trade.position_list_query(trd_env=TrdEnv.SIMULATE, acc_id=acct)
        held = positions[(positions.code.astype(str) == SYMBOL) & (positions.qty.astype(float) != 0)] if ret == RET_OK and not positions.empty else positions.iloc[0:0]
        ret, orders = trade.order_list_query(trd_env=TrdEnv.SIMULATE, acc_id=acct)
        ant_orders = orders[orders.code.astype(str) == SYMBOL] if ret == RET_OK and not orders.empty else orders.iloc[0:0]
        active = ant_orders[~ant_orders.order_status.astype(str).str.upper().isin(TERMINAL)] if not ant_orders.empty else ant_orders
        ret, snapshot = quote.get_market_snapshot([SYMBOL])
        quote_row = snapshot.iloc[0] if ret == RET_OK and not snapshot.empty else None
        with sqlite3.connect(ROOT / "quant_dashboard.sqlite3") as db:
            cache_end = db.execute("select max(time_key) from candles where symbol=? and interval_minutes=1", (SYMBOL,)).fetchone()[0]
        bot_state = state_path(SYMBOL)
        state = json.loads(bot_state.read_text()) if bot_state.exists() else {}
        return {
            "time": dt.datetime.now(ET).isoformat(timespec="seconds"),
            "phase": session_phase(dt.datetime.now(ET).date(), dt.datetime.now(ET).time()),
            "account": acct, "environment": env,
            "bot_count": process_count("competition_bot.py"),
            "research_count": process_count("continuous_research_scheduler.py"),
            "qty": int(float(held.iloc[0].qty)) if not held.empty else 0,
            "avg": float(held.iloc[0].cost_price) if not held.empty else 0.0,
            "position_side": str(held.iloc[0].position_side) if not held.empty and "position_side" in held.columns else "FLAT",
            "active_orders": len(active),
            "bid": float(quote_row.bid_price) if quote_row is not None else None,
            "ask": float(quote_row.ask_price) if quote_row is not None else None,
            "cache_end": str(cache_end),
            "state_pending": state.get("pending", {}),
            "state_interval": state.get("strategy_interval"),
        }
    finally:
        quote.close(); trade.close()


def main() -> int:
    if "--once" in sys.argv:
        try:
            value = check()
            fault = (
                value["environment"] != "SIMULATE"
                or value["bot_count"] != 1
                or value["research_count"] > 1
                or value["qty"] < 0
            )
            value["state"] = "FAULT" if fault else "HEALTHY"
            print(json.dumps(value, sort_keys=True), flush=True)
            return 0 if not fault else 1
        except Exception as exc:
            print(json.dumps({"state": "FAULT", "error": repr(exc)}), flush=True)
            return 1
    last = None
    while True:
        now = dt.datetime.now(ET)
        if now.time() >= dt.time(16, 5):
            print(json.dumps({"state": "CLOSED", "time": now.isoformat(timespec="seconds")}), flush=True)
            return 0
        try:
            value = check()
            fault = (
                value["environment"] != "SIMULATE"
                or value["bot_count"] != 1
                or value["research_count"] > 1
                or value["qty"] < 0
            )
            value["state"] = "FAULT" if fault else "HEALTHY"
            compact = json.dumps(value, sort_keys=True, separators=(",", ":"))
            if compact != last or now.minute % 5 == 0:
                print(compact, flush=True)
            last = compact
        except Exception as exc:
            print(json.dumps({"state": "FAULT", "error": repr(exc), "time": now.isoformat(timespec="seconds")}), flush=True)
        time.sleep(60)


if __name__ == "__main__":
    raise SystemExit(main())
