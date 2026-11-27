"""Single source of truth for the paper-trading portfolio and local paths."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "trading_system.json"
_SYMBOL = re.compile(r"^US\.[A-Z][A-Z0-9.-]{0,14}$")


def normalize_symbol(value: Any) -> str:
    symbol = str(value or "").upper().strip()
    if symbol and "." not in symbol:
        symbol = f"US.{symbol}"
    if not _SYMBOL.fullmatch(symbol):
        raise ValueError(f"invalid US symbol: {value!r}")
    return symbol


def load_config() -> dict[str, Any]:
    payload = json.loads(CONFIG_PATH.read_text())
    configured = payload.get("active_symbols") or [payload.get("active_symbol")]
    symbols = tuple(dict.fromkeys(normalize_symbol(value) for value in configured))
    if not symbols:
        raise ValueError("active_symbols must contain at least one US symbol")
    symbol = symbols[0]
    if payload.get("market") != "US" or payload.get("mode") != "SIMULATE":
        raise ValueError("trading system must remain US and SIMULATE")
    intervals = tuple(int(value) for value in payload.get("minute_intervals", []))
    if intervals != tuple(range(1, 11)):
        raise ValueError("minute_intervals must be exactly 1 through 10")
    return {**payload, "active_symbol": symbol, "active_symbols": symbols, "minute_intervals": intervals}


def active_symbol() -> str:
    return str(load_config()["active_symbol"])


def active_symbols() -> tuple[str, ...]:
    return tuple(load_config()["active_symbols"])


def symbol_slug(symbol: str | None = None) -> str:
    return normalize_symbol(symbol or active_symbol()).replace(".", "-")


def symbol_dir(kind: str, symbol: str | None = None) -> Path:
    path = ROOT / kind / symbol_slug(symbol)
    path.mkdir(parents=True, exist_ok=True)
    return path


def strategy_path(symbol: str | None = None) -> Path:
    return symbol_dir("strategies", symbol) / "paper.json"


def state_path(symbol: str | None = None) -> Path:
    return symbol_dir("state", symbol) / "bot.json"


def promotion_path(symbol: str | None = None) -> Path:
    return symbol_dir("promotions", symbol) / "pending.json"


def research_dir(symbol: str | None = None) -> Path:
    return symbol_dir("research", symbol)


def write_active_symbol(symbol: str) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    payload = load_config()
    payload["active_symbol"] = normalized
    payload["active_symbols"] = [normalized]
    payload["minute_intervals"] = list(payload["minute_intervals"])
    temporary = CONFIG_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(CONFIG_PATH)
    return payload


def write_active_symbols(symbols: list[str]) -> dict[str, Any]:
    normalized = list(dict.fromkeys(normalize_symbol(symbol) for symbol in symbols))
    if not normalized:
        raise ValueError("at least one symbol is required")
    payload = load_config()
    payload["active_symbol"] = normalized[0]
    payload["active_symbols"] = normalized
    payload["minute_intervals"] = list(payload["minute_intervals"])
    temporary = CONFIG_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(CONFIG_PATH)
    return payload
