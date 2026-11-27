#!/usr/bin/env python3
"""SQLite persistence for Moomoo candles, research runs, and paper events."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from trading_config import normalize_symbol


DB_PATH = Path(__file__).with_name("quant_dashboard.sqlite3")


def _json(value: Any) -> str:
    if is_dataclass(value):
        value = asdict(value)
    return json.dumps(value, default=str, sort_keys=True, allow_nan=False)


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS candles (
            symbol TEXT NOT NULL,
            interval_minutes INTEGER NOT NULL,
            time_key TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL,
            turnover REAL,
            source TEXT NOT NULL DEFAULT 'moomoo_opend',
            PRIMARY KEY (symbol, interval_minutes, time_key)
        );
        CREATE INDEX IF NOT EXISTS candles_lookup ON candles(symbol, interval_minutes, time_key);
        CREATE TABLE IF NOT EXISTS backtest_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            symbol TEXT NOT NULL,
            interval_minutes INTEGER NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            params_json TEXT NOT NULL,
            stats_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS backtest_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
            trade_index INTEGER NOT NULL,
            trade_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS optimizer_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
            rank INTEGER NOT NULL,
            params_json TEXT NOT NULL,
            stats_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS execution_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            symbol TEXT,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS exit_evaluations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            symbol TEXT NOT NULL,
            position_shares REAL NOT NULL,
            avg_cost REAL NOT NULL,
            price_t0 REAL NOT NULL,
            unrealized_pl REAL NOT NULL,
            return_pct REAL NOT NULL,
            hold_minutes REAL NOT NULL,
            features_json TEXT NOT NULL,
            predicted_action TEXT NOT NULL,
            confidence REAL NOT NULL,
            signal_type TEXT NOT NULL,
            reason TEXT NOT NULL,
            model TEXT NOT NULL,
            provider TEXT NOT NULL,
            price_t1m REAL,
            price_t5m REAL,
            price_t15m REAL,
            optimal_action TEXT,
            counterfactual_reward REAL,
            resolved_at TEXT
        );
        CREATE INDEX IF NOT EXISTS exit_evaluations_lookup ON exit_evaluations(symbol, created_at);
        CREATE INDEX IF NOT EXISTS exit_evaluations_unresolved ON exit_evaluations(resolved_at);
        """
    )
    return db


def save_candles(frame: pd.DataFrame, symbol: str, interval_minutes: int, source: str = "moomoo_opend") -> int:
    normalized_symbol = normalize_symbol(symbol)
    db = connect()
    count = 0
    try:
        rows = []
        # itertuples avoids constructing a pandas Series for every candle.
        # The storage schema is explicit, so named tuple access is both safe
        # and materially cheaper for the repeated live polling path.
        columns = set(frame.columns)
        for row in frame.itertuples(index=True, name="CandleRow"):
            stamp = row.Index
            values = row._asdict()
            rows.append((
                normalized_symbol, interval_minutes, pd.Timestamp(stamp).isoformat(),
                float(values["open"]), float(values["high"]), float(values["low"]), float(values["close"]),
                float(values["volume"]) if "volume" in columns and pd.notna(values["volume"]) else None,
                float(values["turnover"]) if "turnover" in columns and pd.notna(values["turnover"]) else None,
                source,
            ))
        db.executemany(
            """INSERT OR REPLACE INTO candles
            (symbol, interval_minutes, time_key, open, high, low, close, volume, turnover, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows,
        )
        db.commit()
        count = len(rows)
    finally:
        db.close()
    return count


def load_candles(symbol: str, interval_minutes: int, start: str, end: str) -> pd.DataFrame:
    db = connect()
    try:
        frame = pd.read_sql_query(
            """SELECT time_key, open, high, low, close, volume, turnover
               FROM candles
               WHERE symbol = ? AND interval_minutes = ? AND time_key >= ? AND time_key <= ?
               ORDER BY time_key""",
            db, params=(symbol, interval_minutes, start, end), parse_dates=["time_key"],
        )
    finally:
        db.close()
    if frame.empty:
        return frame
    return frame.set_index("time_key").dropna(subset=["open", "high", "low", "close"])


def save_backtest(symbol: str, interval_minutes: int, start: str, end: str, params: dict, stats: dict, trades: list[Any], optimization: list[dict]) -> int:
    db = connect()
    try:
        cur = db.execute(
            "INSERT INTO backtest_runs (created_at, symbol, interval_minutes, start_time, end_time, params_json, stats_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), symbol, interval_minutes, start, end, _json(params), _json(stats)),
        )
        run_id = int(cur.lastrowid)
        db.executemany(
            "INSERT INTO backtest_trades (run_id, trade_index, trade_json) VALUES (?, ?, ?)",
            [(run_id, i, _json(trade)) for i, trade in enumerate(trades)],
        )
        db.executemany(
            "INSERT INTO optimizer_results (run_id, rank, params_json, stats_json) VALUES (?, ?, ?, ?)",
            [(run_id, i + 1, _json(row["params"]), _json(row["stats"])) for i, row in enumerate(optimization)],
        )
        db.commit()
        return run_id
    finally:
        db.close()


def save_execution_event(event_type: str, symbol: str | None, payload: Any) -> None:
    db = connect()
    try:
        db.execute(
            "INSERT INTO execution_events (created_at, symbol, event_type, payload_json) VALUES (?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), symbol, event_type, _json(payload)),
        )
        db.commit()
    finally:
        db.close()


def save_exit_evaluation(data: dict[str, Any]) -> int:
    db = connect()
    try:
        cur = db.execute(
            """INSERT INTO exit_evaluations
            (created_at, symbol, position_shares, avg_cost, price_t0, unrealized_pl, return_pct,
             hold_minutes, features_json, predicted_action, confidence, signal_type, reason,
             model, provider)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data.get("created_at") or datetime.now(timezone.utc).isoformat(),
                normalize_symbol(data["symbol"]),
                float(data.get("position_shares", 0.0)),
                float(data.get("avg_cost", 0.0)),
                float(data.get("price_t0", 0.0)),
                float(data.get("unrealized_pl", 0.0)),
                float(data.get("return_pct", 0.0)),
                float(data.get("hold_minutes", 0.0)),
                _json(data.get("features", {})),
                str(data.get("predicted_action", "HOLD")),
                float(data.get("confidence", 0.0)),
                str(data.get("signal_type", "HOLD")),
                str(data.get("reason", "")),
                str(data.get("model", "auto")),
                str(data.get("provider", "local")),
            ),
        )
        db.commit()
        return int(cur.lastrowid)
    finally:
        db.close()


def load_historical_analogues(symbol: str, limit: int = 3) -> list[dict[str, Any]]:
    normalized = normalize_symbol(symbol)
    db = connect()
    try:
        rows = db.execute(
            """SELECT symbol, return_pct, hold_minutes, predicted_action, optimal_action,
                      counterfactual_reward, signal_type, reason, created_at
               FROM exit_evaluations
               WHERE symbol = ? AND resolved_at IS NOT NULL
               ORDER BY id DESC LIMIT ?""",
            (normalized, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()

