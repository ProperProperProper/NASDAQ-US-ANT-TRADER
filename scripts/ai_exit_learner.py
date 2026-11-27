#!/usr/bin/env python3
"""AI Exit Learner & Counterfactual Reinforcement Engine.

Evaluates live open positions through the Multi-Model AI Gateway, logs structured
micro-state telemetry, tracks forward counterfactual outcomes (T+1m, T+5m, T+15m),
and maintains an in-context learning memory to continuously improve exit decisions.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import plistlib
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quant_storage import connect, DB_PATH, normalize_symbol, save_exit_evaluation, load_historical_analogues
from trading_config import active_symbols, strategy_path

GATEWAY_URL = "http://127.0.0.1:8788"
CALIBRATION_PATH = ROOT / "data" / "ai_exit_calibration.json"


def account_id() -> int:
    val = os.environ.get("PAPER_ACCOUNT_ID")
    if val:
        return int(val)
    plist_path = Path.home() / "Library/LaunchAgents/com.local.trading-engine.plist"
    if plist_path.exists():
        with plist_path.open("rb") as f:
            configured = plistlib.load(f).get("EnvironmentVariables", {})
            if "PAPER_ACCOUNT_ID" in configured:
                return int(configured["PAPER_ACCOUNT_ID"])
    return 529735


def query_gateway_chat(payload: dict[str, Any], qos: str = "background") -> dict[str, Any]:
    """Send evaluation request to Multi-Model AI Gateway with background E-core QoS."""
    req = Request(
        f"{GATEWAY_URL}/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-qos": qos},
    )
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def extract_position_features(
    symbol: str,
    shares: float,
    avg_cost: float,
    current_price: float,
    snapshot: dict[str, Any],
    strategy_params: dict[str, Any],
    hold_minutes: float = 0.0,
    peak_price: float = 0.0,
    candles: Any = None,
) -> dict[str, Any]:
    """Construct a clean, normalized quantitative feature vector with convergence dynamics."""
    if peak_price <= 0:
        peak_price = max(avg_cost, current_price)
    unrealized_pl = (current_price - avg_cost) * shares
    return_pct = ((current_price - avg_cost) / avg_cost) * 100.0 if avg_cost > 0 else 0.0
    drawdown_from_peak = ((current_price - peak_price) / peak_price) * 100.0 if peak_price > 0 else 0.0

    tp_pct = strategy_params.get("take_profit_pct", 0.007) * 100.0
    sl_pct = strategy_params.get("stop_loss_pct", 0.1324) * 100.0
    dist_to_tp = tp_pct - return_pct
    dist_to_sl = return_pct - (-sl_pct)

    bid = float(snapshot.get("bid_price", current_price) or current_price)
    ask = float(snapshot.get("ask_price", current_price) or current_price)
    spread = max(0.0, ask - bid)
    spread_pct = (spread / current_price) * 100.0 if current_price > 0 else 0.0

    bid_ask_ratio = float(snapshot.get("bid_ask_ratio", 50.0) or 50.0)
    volume_ratio = float(snapshot.get("volume_ratio", 1.0) or 1.0)
    amplitude = float(snapshot.get("amplitude", 0.0) or 0.0)
    turnover_rate = float(snapshot.get("turnover_rate", 0.0) or 0.0)

    vwap = float(snapshot.get("avg_price", current_price) or current_price)
    dist_to_vwap_pct = ((current_price - vwap) / vwap) * 100.0 if vwap > 0 else 0.0

    convergence_telemetry = {}
    if candles is not None and hasattr(candles, "__len__") and len(candles) >= 5:
        try:
            closes = candles["close"].astype(float)
            ema_fast = closes.ewm(span=3, adjust=False).mean()
            ema_slow = closes.ewm(span=8, adjust=False).mean()
            diff = ema_fast - ema_slow
            peak_diff = float(diff.tail(5).max())
            curr_diff = float(diff.iloc[-1])
            prev_diff = float(diff.iloc[-2]) if len(diff) >= 2 else curr_diff
            diff_vel = curr_diff - prev_diff
            ema_decay = (peak_diff - curr_diff) / peak_diff if peak_diff > 1e-4 else (1.0 if curr_diff < 0 else 0.0)

            grid_lookback = max(2, min(240, int(strategy_params.get("grid_lookback", 45))))
            prior_high = float(candles["high"].astype(float).tail(grid_lookback).max())
            prior_low = float(candles["low"].astype(float).tail(grid_lookback).min())
            span = prior_high - prior_low
            exit_mode = str(strategy_params.get("exit_mode", "any_grid_cross_down"))
            exit_level = prior_low + span * 0.25 if "lower_half" in exit_mode or "any_grid" in exit_mode else prior_low
            curr_dist = current_price - exit_level
            prev_dist = float(candles["close"].iloc[-2]) - exit_level if len(candles) >= 2 else curr_dist
            grid_vel = curr_dist - prev_dist
            bars_to_cross = (curr_dist / abs(grid_vel)) if (curr_dist > 0 and grid_vel < 0 and abs(grid_vel) > 1e-4) else (0.0 if curr_dist <= 0 else 99.0)

            profit_giveback = ((peak_price - current_price) / (peak_price - avg_cost) * 100.0) if (peak_price > avg_cost and peak_price - avg_cost > 1e-5) else 0.0

            convergence_telemetry = {
                "fast_slow_ema_gap": round(curr_diff, 4),
                "ema_decay_pct": round(ema_decay * 100.0, 1),
                "ema_gap_velocity": round(diff_vel, 4),
                "bars_to_grid_collision": round(bars_to_cross, 1) if bars_to_cross < 50 else ">10",
                "dist_to_vwap_pct": round(dist_to_vwap_pct, 3),
                "bid_ask_ratio": round(bid_ask_ratio, 1),
                "profit_giveback_pct": round(profit_giveback, 1),
            }
        except Exception as exc:
            logging.debug("convergence feature calculation failed: %s", exc)

    feat = {
        "symbol": normalize_symbol(symbol),
        "shares": shares,
        "avg_cost": round(avg_cost, 4),
        "current_price": round(current_price, 4),
        "unrealized_pl": round(unrealized_pl, 2),
        "return_pct": round(return_pct, 4),
        "hold_minutes": round(hold_minutes, 1),
        "peak_price": round(peak_price, 4),
        "peak_drawdown_pct": round(drawdown_from_peak, 4),
        "dist_to_tp_pct": round(dist_to_tp, 4),
        "dist_to_sl_pct": round(dist_to_sl, 4),
        "bid": round(bid, 4),
        "ask": round(ask, 4),
        "spread_pct": round(spread_pct, 4),
        "bid_ask_ratio": round(bid_ask_ratio, 2),
        "volume_ratio": round(volume_ratio, 2),
        "amplitude": round(amplitude, 2),
        "turnover_rate": round(turnover_rate, 4),
        "dist_to_vwap_pct": round(dist_to_vwap_pct, 4),
    }
    if convergence_telemetry:
        feat["indicator_convergence"] = convergence_telemetry
    return feat


def evaluate_exit(
    features: dict[str, Any],
    analogues: list[dict[str, Any]] | None = None,
    provider: str = "auto",
) -> dict[str, Any]:
    """Query the AI Gateway for an exit evaluation with in-context learning analogues."""
    if analogues is None:
        analogues = load_historical_analogues(features["symbol"], limit=3)

    system_prompt = (
        "You are Multi-Model AI Gateway's quantitative trade exit arbiter with reinforcement memory. "
        "Evaluate the live trade micro-state and dynamic indicator convergence trajectory. "
        "You must determine whether indicator convergence (fast/slow EMAs, price-to-grid collision, VWAP, order-book depth) "
        "represents FATAL LOSS-INVERSION (where holding will surrender profit or cause a loss) vs HEALTHY CONSOLIDATION (hold runner). "
        "Learn from past in-context historical trade outcomes to calibrate your judgment. "
        "Output ONLY valid JSON matching this exact schema: "
        '{"action": "HOLD"|"EXIT", "confidence": 0.0-1.0, '
        '"signal_type": "CONVERGENCE_PROFIT_LOCK"|"CONVERGENCE_LOSS_MITIGATION"|"CONSOLIDATION_HOLD"|"RUNNER_EXTENSION"|"STALL_EXHAUSTION"|"PREVENTIVE_CUT", '
        '"reason": "<brief_technical_reason_under_15_words>"}. '
        "Do NOT provide prose, advice, or conversational disclaimers."
    )

    user_payload = {
        "current_setup": features,
        "in_context_historical_outcomes": [
            {
                "setup_return": f"{a.get('return_pct', 0):+.2f}%",
                "hold_minutes": a.get("hold_minutes", 0),
                "predicted_action": a.get("predicted_action"),
                "optimal_action": a.get("optimal_action"),
                "reward": a.get("counterfactual_reward"),
                "convergence_setup": a.get("features", {}).get("indicator_convergence", {}),
                "learning_lesson": (
                    "EXIT saved profit ahead of collapse" if float(a.get("counterfactual_reward", 0) or 0) > 0 and a.get("predicted_action") == "EXIT"
                    else "HOLD captured runner gains" if float(a.get("counterfactual_reward", 0) or 0) > 0 and a.get("predicted_action") == "HOLD"
                    else "Premature EXIT; trade continued upward" if float(a.get("counterfactual_reward", 0) or 0) < 0 and a.get("predicted_action") == "EXIT"
                    else "Failed to EXIT; trade reversed into loss"
                ),
                "reason": a.get("reason"),
            }
            for a in analogues
        ],
    }

    payload = {
        "model": "auto",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload)},
        ],
        "max_tokens": 128,
        "chat_template_kwargs": {"enable_thinking": False, "reasoning_effort": "low"},
        "extra_body": {
            "provider": provider,
            "qos": "background",
            "bounded_controller": True,
        },
    }

    try:
        response = query_gateway_chat(payload, qos="background")
        raw_text = (
            response.get("choices", [{}])[0].get("message", {}).get("content") or ""
        ).strip()
        # Parse JSON defensively
        decoder = json.JSONDecoder()
        parsed = None
        for i, ch in enumerate(raw_text):
            if ch == "{":
                try:
                    obj, _ = decoder.raw_decode(raw_text[i:])
                    if isinstance(obj, dict) and "action" in obj:
                        parsed = obj
                        break
                except Exception:
                    continue
        if not parsed:
            parsed = {
                "action": "HOLD",
                "confidence": 0.5,
                "signal_type": "HOLD_NORMAL",
                "reason": "fallback_unparsed_response",
            }
        routing = response.get("_multi_model_gateway", {})
        return {
            "decision": parsed,
            "model": response.get("model", "auto"),
            "provider": routing.get("provider", provider),
            "latency_ms": routing.get("latency_ms", 0.0),
            "qos": routing.get("qos", "background"),
            "thermodynamic_offload": routing.get("thermodynamic_offload", False),
        }
    except Exception as exc:
        logging.error("AI gateway query failed: %s", exc)
        return {
            "decision": {
                "action": "HOLD",
                "confidence": 0.5,
                "signal_type": "HOLD_NORMAL",
                "reason": f"gateway_error: {exc}",
            },
            "model": "error",
            "provider": provider,
            "latency_ms": 0.0,
            "qos": "background",
            "thermodynamic_offload": False,
        }


def log_and_save_evaluation(features: dict[str, Any], evaluation: dict[str, Any]) -> int:
    """Save live evaluation to SQLite for counterfactual tracking."""
    decision = evaluation.get("decision", {})
    record = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "symbol": features["symbol"],
        "position_shares": features["shares"],
        "avg_cost": features["avg_cost"],
        "price_t0": features["current_price"],
        "unrealized_pl": features["unrealized_pl"],
        "return_pct": features["return_pct"],
        "hold_minutes": features["hold_minutes"],
        "features": features,
        "predicted_action": decision.get("action", "HOLD"),
        "confidence": decision.get("confidence", 0.5),
        "signal_type": decision.get("signal_type", "HOLD_NORMAL"),
        "reason": decision.get("reason", ""),
        "model": evaluation.get("model", "auto"),
        "provider": evaluation.get("provider", "local"),
    }
    return save_exit_evaluation(record)


def resolve_counterfactual_outcomes(min_age_minutes: int = 15) -> int:
    """Evaluate completed evaluations against subsequent candle prices to compute rewards.

    Ground truth logic:
      - If price dropped >0.30% over the next 15m, EXIT was optimal (+1.0 reward).
      - If price rose >0.30% over the next 15m, HOLD was optimal (+1.0 reward for HOLD, -1.0 for EXIT).
      - Otherwise, outcome was NEUTRAL (0.0 reward).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=min_age_minutes)).isoformat()
    db = connect()
    resolved_count = 0
    try:
        rows = db.execute(
            """SELECT id, symbol, created_at, price_t0, predicted_action
               FROM exit_evaluations
               WHERE resolved_at IS NULL AND created_at <= ?
               ORDER BY id ASC LIMIT 100""",
            (cutoff,),
        ).fetchall()

        for row in rows:
            eval_id = row["id"]
            symbol = row["symbol"]
            t0_str = row["created_at"]
            price_t0 = row["price_t0"]
            predicted = row["predicted_action"]

            t0 = datetime.fromisoformat(t0_str)
            t1 = (t0 + timedelta(minutes=1)).isoformat()
            t5 = (t0 + timedelta(minutes=5)).isoformat()
            t15 = (t0 + timedelta(minutes=15)).isoformat()

            candles = db.execute(
                """SELECT time_key, close
                   FROM candles
                   WHERE symbol = ? AND interval_minutes = 1 AND time_key >= ? AND time_key <= ?
                   ORDER BY time_key ASC""",
                (symbol, t0_str, t15),
            ).fetchall()

            if not candles:
                continue

            candle_map = {c["time_key"]: c["close"] for c in candles}
            closes = [c["close"] for c in candles]
            price_t15m = closes[-1] if closes else price_t0
            price_t5m = closes[min(4, len(closes) - 1)] if closes else price_t0
            price_t1m = closes[0] if closes else price_t0

            fwd_return_15m = ((price_t15m - price_t0) / price_t0) * 100.0

            if fwd_return_15m < -0.30:
                optimal_action = "EXIT"
            elif fwd_return_15m > 0.30:
                optimal_action = "HOLD"
            else:
                optimal_action = "NEUTRAL"

            if optimal_action == "NEUTRAL":
                reward = 0.0
            elif predicted == optimal_action:
                reward = 1.0
            else:
                reward = -1.0

            db.execute(
                """UPDATE exit_evaluations
                   SET price_t1m = ?, price_t5m = ?, price_t15m = ?,
                       optimal_action = ?, counterfactual_reward = ?, resolved_at = ?
                   WHERE id = ?""",
                (price_t1m, price_t5m, price_t15m, optimal_action, reward, datetime.now(timezone.utc).isoformat(), eval_id),
            )
            resolved_count += 1

        db.commit()
    finally:
        db.close()
    return resolved_count


def calibrate_confidence_threshold() -> dict[str, Any]:
    """Analyze resolved evaluations to calculate optimal action threshold."""
    db = connect()
    try:
        rows = db.execute(
            """SELECT confidence, predicted_action, optimal_action, counterfactual_reward
               FROM exit_evaluations
               WHERE resolved_at IS NOT NULL AND optimal_action != 'NEUTRAL'"""
        ).fetchall()
        if len(rows) < 10:
            return {
                "samples": len(rows),
                "status": "INSUFFICIENT_DATA",
                "recommended_exit_threshold": 0.80,
                "message": "Collecting more evaluations for statistical significance (minimum 10 required)",
            }

        data = [dict(r) for r in rows]
        df = pd.DataFrame(data)

        # Search optimal confidence threshold that maximizes exit precision
        best_threshold = 0.75
        best_precision = 0.0
        for th in np.arange(0.50, 0.95, 0.05):
            subset = df[(df["predicted_action"] == "EXIT") & (df["confidence"] >= th)]
            if len(subset) > 0:
                precision = (subset["optimal_action"] == "EXIT").mean()
                if precision > best_precision:
                    best_precision = precision
                    best_threshold = float(th)

        result = {
            "samples": len(df),
            "status": "CALIBRATED",
            "recommended_exit_threshold": best_threshold,
            "exit_precision": round(best_precision, 4),
            "calibrated_at": datetime.now(timezone.utc).isoformat(),
        }
        CALIBRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
        CALIBRATION_PATH.write_text(json.dumps(result, indent=2))
        return result
    finally:
        db.close()


def evaluate_live_positions(interactive: bool = True) -> list[dict[str, Any]]:
    """Inspect all active positions from Moomoo OpenD and run AI evaluation."""
    from moomoo import OpenQuoteContext, OpenSecTradeContext, RET_OK, TrdEnv, TrdMarket

    symbols = active_symbols()
    acct = account_id()
    quote = OpenQuoteContext(host="127.0.0.1", port=11111)
    trade = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host="127.0.0.1", port=11111)

    evaluations = []
    try:
        ret_pos, pos_df = trade.position_list_query(trd_env=TrdEnv.SIMULATE, acc_id=acct)
        if ret_pos != RET_OK or pos_df.empty:
            if interactive:
                print("No open positions found in paper trading account.")
            return []

        us_pos = pos_df[pos_df["code"].astype(str).str.startswith("US.")]
        active_pos = us_pos[us_pos["qty"] > 0]
        if active_pos.empty:
            if interactive:
                print("All active US paper positions are flat.")
            return []

        ret_snap, snap_df = quote.get_market_snapshot(active_pos["code"].tolist())
        snap_map = {}
        if ret_snap == RET_OK and not snap_df.empty:
            for _, row in snap_df.iterrows():
                snap_map[normalize_symbol(row["code"])] = row.to_dict()

        for _, pos in active_pos.iterrows():
            sym = normalize_symbol(pos["code"])
            shares = float(pos["qty"])
            cost = float(pos["cost_price"])
            price = float(pos.get("nominal_price", cost))
            snapshot = snap_map.get(sym, {})

            params = {}
            st_path = strategy_path(sym)
            if st_path.exists():
                try:
                    params = json.loads(st_path.read_text()).get("params", {})
                except Exception:
                    pass

            features = extract_position_features(
                symbol=sym,
                shares=shares,
                avg_cost=cost,
                current_price=price,
                snapshot=snapshot,
                strategy_params=params,
            )

            analogues = load_historical_analogues(sym, limit=3)
            eval_result = evaluate_exit(features, analogues=analogues)
            eval_id = log_and_save_evaluation(features, eval_result)

            decision = eval_result["decision"]
            summary = {
                "id": eval_id,
                "symbol": sym,
                "shares": shares,
                "avg_cost": cost,
                "current_price": price,
                "return_pct": features["return_pct"],
                "action": decision.get("action"),
                "confidence": decision.get("confidence"),
                "signal_type": decision.get("signal_type"),
                "reason": decision.get("reason"),
                "model": eval_result.get("model"),
                "provider": eval_result.get("provider"),
                "latency_ms": eval_result.get("latency_ms"),
                "analogues_injected": len(analogues),
            }
            evaluations.append(summary)

            if interactive:
                print(f"[{sym}] Shares: {int(shares)} | Cost: ${cost:.2f} | Current: ${price:.2f} | P/L: {features['return_pct']:+.2f}%")
                print(f"       AI Decision: {decision.get('action')} ({decision.get('signal_type')}) | Confidence: {decision.get('confidence'):.2f}")
                print(f"       Reason: {decision.get('reason')} (via {eval_result.get('model')})")
                print(f"       Historical Analogues Used: {len(analogues)} | Logged ID: #{eval_id}\n")

    finally:
        quote.close()
        trade.close()

    return evaluations


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Exit Learner & Counterfactual Engine")
    parser.add_argument("--evaluate", action="store_true", help="Evaluate all active live open positions")
    parser.add_argument("--resolve", action="store_true", help="Resolve counterfactual rewards on pending evaluations")
    parser.add_argument("--calibrate", action="store_true", help="Train confidence calibration model on resolved evaluations")
    parser.add_argument("--status", action="store_true", help="Display summary of historical evaluations and rewards")
    args = parser.parse_args()

    if args.evaluate or len(sys.argv) == 1:
        print("=== AI Exit Learner: Evaluating Live Positions ===")
        evaluate_live_positions(interactive=True)

    if args.resolve:
        print("=== Resolving Counterfactual Outcomes ===")
        resolved = resolve_counterfactual_outcomes()
        print(f"Resolved {resolved} pending evaluation(s).")

    if args.calibrate:
        print("=== Calibrating ML Confidence Thresholds ===")
        calib = calibrate_confidence_threshold()
        print(json.dumps(calib, indent=2))

    if args.status:
        db = connect()
        try:
            total = db.execute("SELECT COUNT(*) FROM exit_evaluations").fetchone()[0]
            resolved = db.execute("SELECT COUNT(*) FROM exit_evaluations WHERE resolved_at IS NOT NULL").fetchone()[0]
            rewards = db.execute(
                "SELECT AVG(counterfactual_reward) FROM exit_evaluations WHERE resolved_at IS NOT NULL"
            ).fetchone()[0]
            print(f"Total Evaluations: {total}")
            print(f"Resolved Counterfactuals: {resolved}")
            print(f"Average Reward Score: {rewards if rewards is not None else 'N/A'}")
        finally:
            db.close()


if __name__ == "__main__":
    main()
