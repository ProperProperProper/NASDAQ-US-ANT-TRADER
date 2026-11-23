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
from strategy_57pct import BASELINE, strategy_signal, walk_forward_optimize, walk_forward_replay, stats


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


LIVE_STRATEGY_PATH = Path(__file__).with_name("paper_bot_live_strategy.json")


def write_live_strategy(symbol: str, interval: int, params: dict[str, Any], run_id: str | None = None) -> dict[str, Any]:
    """Publish the latest ANT research parameters for the SIMULATE bot."""
    payload = {
        "symbol": symbol,
        "interval": int(interval),
        "params": dict(params),
        "run_id": run_id,
        "updated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "mode": "SIMULATE",
    }
    temporary = LIVE_STRATEGY_PATH.with_name(LIVE_STRATEGY_PATH.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(LIVE_STRATEGY_PATH)
    return payload


def risk_json(**kwargs: Any) -> dict[str, Any]:
    return asdict(risk_api.entry_decision(**kwargs))
