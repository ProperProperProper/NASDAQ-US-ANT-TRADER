"""Stable local interfaces shared by the native app, bot and research.

The bot and research code call these objects directly; no web service is required.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import json
from pathlib import Path
from typing import Any

import pandas as pd

from quant_storage import load_candles, save_execution_event
from strategy_57pct import BASELINE, strategy_signal, validate_strategy_params, walk_forward_optimize, walk_forward_replay, stats
from trading_config import active_symbols, normalize_symbol, strategy_path


class StrategyAPI:
    name = "GRID_OPEN_CROSS_1D"
    version = "2026-11-13.3"

    def signal(self, candles: pd.DataFrame, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return strategy_signal(candles, {**BASELINE, **(params or {})})

    def walk_forward(self, candles: pd.DataFrame, capital: float, allocation: float,
                     slippage_bps: float, commission: float, trials: int = 12000,
                     progress=None) -> dict[str, Any]:
        folds = walk_forward_optimize(
            candles, BASELINE, capital, allocation, slippage_bps, commission,
            progress=progress, trials=trials,
        )
        _, trades, data = walk_forward_replay(
            candles, folds, capital, allocation, slippage_bps, commission,
        )
        return {"stats": stats(trades, data, capital), "folds": folds,
                "trades": trades, "data": data}


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    quantity: int
    reason: str
    risk_fraction: float


class RiskAPI:
    """Pure, auditable risk calculations; it does not submit orders."""

    def entry_decision(self, cash: float, price: float, allocation: float = .95,
                       max_position_value: float | None = None,
                       drawdown: float = 0.0, max_drawdown: float = .20) -> RiskDecision:
        if price <= 0 or cash <= 0:
            return RiskDecision(False, 0, "invalid_cash_or_price", 0.0)
        if drawdown <= -abs(max_drawdown):
            return RiskDecision(False, 0, "drawdown_circuit_breaker", 0.0)
        value = cash * max(0.0, min(1.0, allocation))
        if max_position_value is not None:
            value = min(value, max(0.0, max_position_value))
        qty = int(value // price)
        if qty <= 0:
            return RiskDecision(False, 0, "insufficient_buying_power", 0.0)
        return RiskDecision(True, qty, "approved", value / cash)


class AuditAPI:
    def event(self, event_type: str, symbol: str | None, payload: Any) -> None:
        save_execution_event(event_type, symbol, payload)


class DataAPI:
    def candles(self, symbol: str, interval: int, start: str, end: str) -> pd.DataFrame:
        return load_candles(symbol, interval, start, end)


strategy_api = StrategyAPI()
risk_api = RiskAPI()
audit_api = AuditAPI()
data_api = DataAPI()


LIVE_STRATEGY_PATH = strategy_path()


def write_live_strategy(symbol: str, interval: int, params: dict[str, Any], run_id: str | None = None) -> dict[str, Any]:
    """Publish a strategy only for the configured SIMULATE paper symbol."""
    symbol = normalize_symbol(symbol)
    if symbol not in active_symbols() or int(interval) not in range(1, 11):
        raise ValueError("paper strategy requires a configured portfolio symbol and a 1–10 minute interval")
    valid_params = validate_strategy_params(params)
    if "take_profit_pct" in valid_params:
        valid_params["take_profit_pct"] = round(float(valid_params["take_profit_pct"]) * 0.70, 6)
    payload = {
        "symbol": symbol,
        "interval": int(interval),
        "params": valid_params,
        "run_id": run_id,
        "updated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "mode": "SIMULATE",
    }
    path = strategy_path(symbol)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)
    return payload


def risk_json(**kwargs: Any) -> dict[str, Any]:
    return asdict(risk_api.entry_decision(**kwargs))
