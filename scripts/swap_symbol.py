#!/usr/bin/env python3
"""Safely change the single active symbol on the standard SIMULATE account."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from moomoo import OpenQuoteContext, OpenSecTradeContext, RET_OK, TrdEnv, TrdMarket
from strategy_57pct import BASELINE
from trading_config import active_symbol, normalize_symbol, strategy_path, write_active_symbol

TERMINAL = {"FILLED_ALL", "CANCELLED_ALL", "CANCELLED_PART", "FAILED", "DELETED", "CANCELLED"}


def account_id() -> int:
    value = os.environ.get("PAPER_ACCOUNT_ID", "0")
    account = int(value or 0)
    if account <= 0:
        raise RuntimeError("PAPER_ACCOUNT_ID is required")
    return account


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("symbol")
    args = parser.parse_args()
    requested = normalize_symbol(args.symbol)
    current = active_symbol()
    account = account_id()
    trade = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host="127.0.0.1", port=11111)
    quote = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        ret, accounts = trade.get_acc_list()
        if ret != RET_OK:
            raise RuntimeError(f"account query failed: {accounts}")
        rows = accounts[accounts["acc_id"].astype(str) == str(account)]
        if rows.empty or str(rows.iloc[0].get("trd_env", "")).upper() != "SIMULATE":
            raise RuntimeError(f"configured account {account} is not SIMULATE")
        ret, positions = trade.position_list_query(trd_env=TrdEnv.SIMULATE, acc_id=account)
        if ret != RET_OK:
            raise RuntimeError(f"position query failed: {positions}")
        guarded = {current, requested}
        if not positions.empty and "code" in positions:
            held = positions[positions["code"].astype(str).isin(guarded) & (positions["qty"].astype(float) != 0)]
            if not held.empty:
                raise RuntimeError(f"symbol swap requires flat positions: {held[['code', 'qty']].to_dict('records')}")
        ret, orders = trade.order_list_query(trd_env=TrdEnv.SIMULATE, acc_id=account)
        if ret != RET_OK:
            raise RuntimeError(f"order query failed: {orders}")
        if not orders.empty and {"code", "order_status"}.issubset(orders.columns):
            active = orders[orders["code"].astype(str).isin(guarded)]
            active = active[~active["order_status"].astype(str).str.upper().isin(TERMINAL)]
            if not active.empty:
                raise RuntimeError(f"symbol swap requires no pending orders: {active[['code', 'order_status']].to_dict('records')}")
        ret, snapshot = quote.get_market_snapshot([requested])
        if ret != RET_OK or snapshot.empty:
            raise RuntimeError(f"Moomoo could not validate {requested}: {snapshot}")
        path = strategy_path(requested)
        if not path.exists():
            payload = {"symbol": requested, "interval": 10, "params": dict(BASELINE),
                       "run_id": "INITIAL-BASELINE", "mode": "SIMULATE"}
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            temporary.replace(path)
        write_active_symbol(requested)
        print(json.dumps({"state": "CHANGED", "old_symbol": current, "active_symbol": requested,
                          "account": account, "mode": "SIMULATE", "strategy": str(path)}, sort_keys=True))
        return 0
    finally:
        quote.close()
        trade.close()


if __name__ == "__main__":
    raise SystemExit(main())
