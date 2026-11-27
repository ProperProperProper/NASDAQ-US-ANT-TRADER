#!/usr/bin/env python3
"""Dynamic AI Exit Arbiter & Quantitative Micro-Structure Execution Engine.

Acts like a professional quantitative execution desk by assessing live price action,
order-book imbalance, VWAP deviation, and peak profit erosion to decide whether
an open position should be exited to lock in profit or cut risk early, REGARDLESS
of whether technical indicators (e.g. grid lines, moving averages) have triggered.

Combines:
1. Wire-speed deterministic micro-structure evaluation (0ms latency, runs every tick)
2. Asynchronous deep Multi-Model AI Gateway reasoning with in-context exemplar memory
3. Structured execution auditing and counterfactual logging to SQLite
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

from quant_storage import save_exit_evaluation, load_historical_analogues
from trading_config import strategy_path


class AIExitArbiter:
    """Evaluates open positions dynamically to protect profits and enforce pro-trader exits."""

    def __init__(
        self,
        gateway_url: str = "http://127.0.0.1:8788",
        min_profit_to_protect_pct: float = 0.20,
        giveback_threshold_pct: float = 35.0,
        min_confidence: float = 0.70,
        enable_ai_gateway: bool = True,
        bg_eval_interval_seconds: float = 20.0,
    ) -> None:
        self.gateway_url = gateway_url
        self.min_profit_to_protect_pct = min_profit_to_protect_pct
        self.giveback_threshold_pct = giveback_threshold_pct
        self.min_confidence = min_confidence
        self.enable_ai_gateway = enable_ai_gateway
        self.bg_eval_interval_seconds = bg_eval_interval_seconds

        self._lock = threading.Lock()
        self._approved_ai_exits: dict[str, dict[str, Any]] = {}
        self._last_ai_eval_time: dict[str, float] = {}
        self._bg_thread: threading.Thread | None = None
        self._running = False

    def evaluate_microstructure_exit(
        self,
        symbol: str,
        price: float,
        qty: float,
        avg_cost: float,
        snapshot: dict[str, Any],
        peak_price: float = 0.0,
        params: dict[str, Any] | None = None,
    ) -> tuple[str | None, dict[str, Any]]:
        """Evaluate deterministic micro-structure rules at wire speed (0ms).

        Returns:
            (exit_reason, telemetry_dict) or (None, telemetry_dict)
        """
        if qty <= 0 or avg_cost <= 0 or price <= 0:
            return None, {}

        params = params or {}
        scaled_tp = float(params.get("take_profit_pct", 0.01918)) * 100.0  # in %
        scaled_sl = float(params.get("stop_loss_pct", 0.1324)) * 100.0     # in %

        if peak_price <= 0:
            peak_price = max(avg_cost, price)
        peak_price = max(peak_price, price)

        return_pct = ((price - avg_cost) / avg_cost) * 100.0
        peak_return_pct = ((peak_price - avg_cost) / avg_cost) * 100.0
        drawdown_from_peak_pct = ((price - peak_price) / peak_price) * 100.0 if peak_price > 0 else 0.0

        profit_giveback_pct = 0.0
        if peak_price > avg_cost and (peak_price - avg_cost) > 1e-6:
            profit_giveback_pct = ((peak_price - price) / (peak_price - avg_cost)) * 100.0

        bid = float(snapshot.get("bid_price", price) or price)
        ask = float(snapshot.get("ask_price", price) or price)
        spread_pct = ((ask - bid) / price) * 100.0 if price > 0 else 0.0
        bid_ask_ratio = float(snapshot.get("bid_ask_ratio", 50.0) or 50.0)
        vwap = float(snapshot.get("avg_price", price) or price)
        dist_to_vwap_pct = ((price - vwap) / vwap) * 100.0 if vwap > 0 else 0.0
        volume_ratio = float(snapshot.get("volume_ratio", 1.0) or 1.0)

        telemetry = {
            "symbol": symbol,
            "shares": qty,
            "avg_cost": round(avg_cost, 4),
            "current_price": round(price, 4),
            "peak_price": round(peak_price, 4),
            "return_pct": round(return_pct, 4),
            "peak_return_pct": round(peak_return_pct, 4),
            "drawdown_from_peak_pct": round(drawdown_from_peak_pct, 4),
            "profit_giveback_pct": round(profit_giveback_pct, 2),
            "dist_to_tp_pct": round(scaled_tp - return_pct, 4),
            "dist_to_sl_pct": round(return_pct - (-scaled_sl), 4),
            "bid_ask_ratio": round(bid_ask_ratio, 2),
            "dist_to_vwap_pct": round(dist_to_vwap_pct, 4),
            "spread_pct": round(spread_pct, 4),
            "volume_ratio": round(volume_ratio, 2),
        }

        # -------------------------------------------------------------
        # Rule 1: DYNAMIC_PROFIT_PROTECT (Lock in banked gain on stall)
        # -------------------------------------------------------------
        tp_progress_ratio = (peak_return_pct / scaled_tp) if scaled_tp > 0 else 0.0
        if (peak_return_pct >= 0.35 or tp_progress_ratio >= 0.45) and return_pct >= self.min_profit_to_protect_pct:
            if profit_giveback_pct >= self.giveback_threshold_pct and (bid_ask_ratio < 44.0 or dist_to_vwap_pct < -0.05):
                logging.info(
                    "AI Exit Triggered [%s]: DYNAMIC_PROFIT_PROTECT (Peak: +%.2f%%, Curr: +%.2f%%, Giveback: %.1f%%, B/A: %.1f)",
                    symbol, peak_return_pct, return_pct, profit_giveback_pct, bid_ask_ratio,
                )
                return "AI_TAKE_PROFIT_STALL", telemetry

        # -------------------------------------------------------------
        # Rule 2: ORDERBOOK_LIQUIDITY_EXHAUSTION (Asks overwhelming bids)
        # -------------------------------------------------------------
        if return_pct >= 0.15 and bid_ask_ratio < 30.0 and dist_to_vwap_pct < 0.0:
            logging.info(
                "AI Exit Triggered [%s]: ORDERBOOK_EXHAUSTION (Curr: +%.2f%%, B/A Ratio: %.1f%%, VWAP Dist: %.2f%%)",
                symbol, return_pct, bid_ask_ratio, dist_to_vwap_pct,
            )
            return "AI_ORDERBOOK_EXHAUSTION", telemetry

        # -------------------------------------------------------------
        # Rule 3: RUNNER_EXTENSION_REVERSAL (Near-TP rejection)
        # -------------------------------------------------------------
        if tp_progress_ratio >= 0.80 and drawdown_from_peak_pct <= -0.25 and return_pct >= 0.25:
            logging.info(
                "AI Exit Triggered [%s]: RUNNER_EXTENSION_REVERSAL (Reached %.1f%% of TP, Drawdown: %.2f%%)",
                symbol, tp_progress_ratio * 100.0, drawdown_from_peak_pct,
            )
            return "AI_TAKE_PROFIT_NEAR_TARGET", telemetry

        # -------------------------------------------------------------
        # Rule 4: PREVENTIVE_RISK_CUT (Early scratch on severe weakness)
        # -------------------------------------------------------------
        if return_pct <= -0.75 and bid_ask_ratio <= 22.0 and dist_to_vwap_pct <= -0.25:
            logging.info(
                "AI Exit Triggered [%s]: PREVENTIVE_CUT (Loss: %.2f%%, B/A: %.1f%%, VWAP Dist: %.2f%%)",
                symbol, return_pct, bid_ask_ratio, dist_to_vwap_pct,
            )
            return "AI_PREVENTIVE_CUT", telemetry

        return None, telemetry

    def evaluate_indicator_convergence(
        self,
        symbol: str,
        price: float,
        qty: float,
        avg_cost: float,
        candles: Any,
        snapshot: dict[str, Any],
        peak_price: float = 0.0,
        params: dict[str, Any] | None = None,
    ) -> tuple[str | None, dict[str, Any]]:
        """Evaluate multi-indicator convergence trajectories to detect loss inversion before it happens.

        Quantifies rate of change, momentum compression (fast/slow EMA decay),
        time-to-collision with grid boundaries, and order-book depth erosion.
        """
        if candles is None or not hasattr(candles, "__len__") or len(candles) < 5:
            return None, {}
        if qty <= 0 or avg_cost <= 0 or price <= 0:
            return None, {}

        params = params or {}
        if peak_price <= 0:
            peak_price = max(avg_cost, price)
        peak_price = max(peak_price, price)

        return_pct = ((price - avg_cost) / avg_cost) * 100.0

        # 1. Momentum Compression: Fast EMA (3) vs Slow EMA (8)
        c_momentum = 0.0
        ema_decay = 0.0
        diff_vel = 0.0
        try:
            closes = candles["close"].astype(float)
            ema_fast = closes.ewm(span=3, adjust=False).mean()
            ema_slow = closes.ewm(span=8, adjust=False).mean()
            diff = ema_fast - ema_slow

            peak_diff = float(diff.tail(5).max())
            curr_diff = float(diff.iloc[-1])
            prev_diff = float(diff.iloc[-2]) if len(diff) >= 2 else curr_diff
            diff_vel = curr_diff - prev_diff
            if peak_diff > 1e-4:
                ema_decay = (peak_diff - curr_diff) / peak_diff
            elif curr_diff < 0:
                ema_decay = 1.0

            # Convergence toward negative crossover:
            if curr_diff < 0:
                c_momentum = 1.0
            elif diff_vel < 0 and ema_decay >= 0.45:
                # Fast line is accelerating downwards into slow line
                c_momentum = min(1.0, max(0.5, ema_decay))
        except Exception as exc:
            logging.debug("momentum convergence check failed: %s", exc)

        # 2. Grid Boundary Convergence & Projected Time-To-Collision
        c_grid = 0.0
        bars_to_cross = 999.0
        try:
            grid_lookback = max(2, min(240, int(params.get("grid_lookback", 45))))
            prior_high = float(candles["high"].astype(float).tail(grid_lookback).max())
            prior_low = float(candles["low"].astype(float).tail(grid_lookback).min())
            span = prior_high - prior_low
            exit_mode = str(params.get("exit_mode", "any_grid_cross_down"))

            if "lower_half" in exit_mode or "any_grid" in exit_mode:
                exit_level = prior_low + span * 0.25
            else:
                exit_level = prior_low

            curr_dist = price - exit_level
            prev_dist = float(candles["close"].iloc[-2]) - exit_level if len(candles) >= 2 else curr_dist
            grid_vel = curr_dist - prev_dist

            if curr_dist <= 0:
                c_grid = 1.0
                bars_to_cross = 0.0
            elif grid_vel < 0:
                bars_to_cross = curr_dist / abs(grid_vel) if abs(grid_vel) > 1e-4 else 999.0
                if bars_to_cross <= 1.8:
                    c_grid = min(1.0, (2.0 - bars_to_cross) / 1.5)
        except Exception as exc:
            logging.debug("grid convergence check failed: %s", exc)

        # 3. Price-to-VWAP Convergence
        c_vwap = 0.0
        vwap = float(snapshot.get("avg_price", price) or price)
        dist_to_vwap_pct = ((price - vwap) / vwap) * 100.0 if vwap > 0 else 0.0
        if dist_to_vwap_pct < 0.0:
            c_vwap = min(1.0, abs(dist_to_vwap_pct) / 0.20)
        elif dist_to_vwap_pct < 0.06 and peak_price > price:
            c_vwap = 0.65

        # 4. Order-Book Liquidity Inversion
        c_book = 0.0
        bid_ask_ratio = float(snapshot.get("bid_ask_ratio", 50.0) or 50.0)
        if bid_ask_ratio < 38.0:
            c_book = min(1.0, (42.0 - bid_ask_ratio) / 20.0)

        # 5. Composite Loss Convergence Score (LCS)
        lcs = (0.35 * c_momentum) + (0.30 * c_grid) + (0.20 * c_vwap) + (0.15 * c_book)

        telemetry = {
            "symbol": symbol,
            "shares": qty,
            "avg_cost": round(avg_cost, 4),
            "current_price": round(price, 4),
            "peak_price": round(peak_price, 4),
            "return_pct": round(return_pct, 4),
            "lcs_score": round(lcs, 4),
            "c_momentum": round(c_momentum, 2),
            "c_grid": round(c_grid, 2),
            "c_vwap": round(c_vwap, 2),
            "c_book": round(c_book, 2),
            "ema_decay_pct": round(ema_decay * 100.0, 1),
            "bars_to_grid_cross": round(bars_to_cross, 2),
            "bid_ask_ratio": round(bid_ask_ratio, 2),
            "dist_to_vwap_pct": round(dist_to_vwap_pct, 4),
        }

        # Trigger if composite loss convergence is elevated (>= 0.65)
        if lcs >= 0.65:
            if return_pct >= 0.10:
                logging.info(
                    "AI Exit Triggered [%s]: CONVERGENCE_PROFIT_LOCK (LCS: %.2f, Return: +%.2f%%, Momentum Decay: %.1f%%, Tau: %.1fb)",
                    symbol, lcs, return_pct, ema_decay * 100.0, bars_to_cross,
                )
                return "AI_CONVERGENCE_PROFIT_LOCK", telemetry
            else:
                logging.info(
                    "AI Exit Triggered [%s]: CONVERGENCE_LOSS_MITIGATION (LCS: %.2f, Return: %.2f%%, Momentum Decay: %.1f%%)",
                    symbol, lcs, return_pct, ema_decay * 100.0,
                )
                return "AI_CONVERGENCE_LOSS_MITIGATION", telemetry

        return None, telemetry

    def check_exit(
        self,
        symbol: str,
        price: float,
        qty: float,
        avg_cost: float,
        snapshot: dict[str, Any],
        peak_price: float = 0.0,
        params: dict[str, Any] | None = None,
        candles: Any = None,
    ) -> tuple[str | None, dict[str, Any]]:
        """Unified exit check: evaluates micro-structure first, then indicator convergence, then AI Gateway."""
        # 1. Wire-Speed Micro-Structure Guard (0ms)
        ms_reason, telemetry = self.evaluate_microstructure_exit(
            symbol=symbol,
            price=price,
            qty=qty,
            avg_cost=avg_cost,
            snapshot=snapshot,
            peak_price=peak_price,
            params=params,
        )
        if ms_reason:
            self._record_decision_event(symbol, ms_reason, telemetry, source="microstructure")
            return ms_reason, telemetry

        # 2. Smart Indicator Convergence Toward Loss (Multi-Indicator Velocity Vector)
        if candles is not None:
            conv_reason, conv_telemetry = self.evaluate_indicator_convergence(
                symbol=symbol,
                price=price,
                qty=qty,
                avg_cost=avg_cost,
                candles=candles,
                snapshot=snapshot,
                peak_price=peak_price,
                params=params,
            )
            if conv_reason:
                self._record_decision_event(symbol, conv_reason, conv_telemetry, source="indicator_convergence")
                return conv_reason, conv_telemetry

        # 3. Approved Asynchronous AI Gateway Decision
        with self._lock:
            approved = self._approved_ai_exits.pop(symbol, None)

        if approved:
            decision = approved.get("decision", {})
            action = decision.get("action", "HOLD")
            confidence = float(decision.get("confidence", 0.0))
            signal_type = decision.get("signal_type", "HOLD_NORMAL")
            reason_text = decision.get("reason", "ai_model_exit")

            if action == "EXIT" and confidence >= self.min_confidence:
                ai_reason = f"AI_{signal_type}"
                logging.info(
                    "AI Gateway Approved Exit [%s]: %s (Confidence: %.2f, Reason: %s)",
                    symbol, ai_reason, confidence, reason_text,
                )
                telemetry["ai_decision"] = decision
                self._record_decision_event(symbol, ai_reason, telemetry, source="ai_gateway", model=approved.get("model"))
                return ai_reason, telemetry

        return None, telemetry

    def approve_ai_exit(self, symbol: str, evaluation: dict[str, Any]) -> None:
        """Publish an approved exit decision from the background AI evaluator."""
        with self._lock:
            self._approved_ai_exits[symbol] = evaluation

    def start_background_evaluator(
        self,
        quote_ctx: Any,
        trade_ctx: Any,
        symbols: list[str],
        acc_id: int | None = None,
    ) -> None:
        """Start non-blocking daemon thread querying the AI Gateway for open positions."""
        if self._bg_thread is not None and self._bg_thread.is_alive():
            return
        if not self.enable_ai_gateway:
            return

        self._running = True
        self._bg_thread = threading.Thread(
            target=self._background_eval_loop,
            args=(quote_ctx, trade_ctx, symbols, acc_id),
            daemon=True,
            name="AIExitEvaluatorThread",
        )
        self._bg_thread.start()
        logging.info("AIExitArbiter background evaluator thread started.")

    def stop_background_evaluator(self) -> None:
        self._running = False

    def _background_eval_loop(
        self,
        quote_ctx: Any,
        trade_ctx: Any,
        symbols: list[str],
        acc_id: int | None = None,
    ) -> None:
        """Periodically evaluate live positions through Multi-Model AI Gateway in background."""
        import sys
        if str(ROOT / "scripts") not in sys.path:
            sys.path.append(str(ROOT / "scripts"))
        try:
            import ai_exit_learner
        except ImportError:
            logging.error("Could not import ai_exit_learner in background thread.")
            return

        from moomoo import TrdEnv, RET_OK

        while self._running:
            try:
                kwargs = {"trd_env": TrdEnv.SIMULATE, "acc_id": acc_id} if acc_id else {"trd_env": TrdEnv.SIMULATE}
                ret, pos_data = trade_ctx.position_list_query(**kwargs)
                if ret == RET_OK and not pos_data.empty:
                    open_rows = pos_data[pos_data["qty"] > 0]
                    for _, row in open_rows.iterrows():
                        code = str(row["code"])
                        if not code.startswith("US."):
                            continue
                        shares = float(row["qty"])
                        cost = float(row["cost_price"])
                        price = float(row.get("nominal_price", cost))

                        # Rate limit AI evaluation per symbol to avoid spamming local LLM
                        now_ts = time.time()
                        last_ts = self._last_ai_eval_time.get(code, 0.0)
                        if now_ts - last_ts < self.bg_eval_interval_seconds:
                            continue
                        self._last_ai_eval_time[code] = now_ts

                        # Get snapshot
                        snap_dict = {}
                        try:
                            s_ret, s_df = quote_ctx.get_market_snapshot([code])
                            if s_ret == RET_OK and not s_df.empty:
                                snap_dict = s_df.iloc[0].to_dict()
                        except Exception:
                            pass

                        # Strategy params
                        params = {}
                        st_path = strategy_path(code)
                        if st_path.exists():
                            try:
                                params = json.loads(st_path.read_text()).get("params", {})
                            except Exception:
                                pass

                        features = ai_exit_learner.extract_position_features(
                            symbol=code,
                            shares=shares,
                            avg_cost=cost,
                            current_price=price,
                            snapshot=snap_dict,
                            strategy_params=params,
                        )

                        analogues = load_historical_analogues(code, limit=3)
                        eval_result = ai_exit_learner.evaluate_exit(features, analogues=analogues)
                        ai_exit_learner.log_and_save_evaluation(features, eval_result)

                        decision = eval_result.get("decision", {})
                        if decision.get("action") == "EXIT" and float(decision.get("confidence", 0.0)) >= self.min_confidence:
                            logging.info(
                                "Background AI Arbiter issued EXIT for %s: %s (conf: %.2f)",
                                code, decision.get("reason"), decision.get("confidence"),
                            )
                            self.approve_ai_exit(code, eval_result)

            except Exception as exc:
                logging.debug("Background AI exit loop error: %s", exc)

            time.sleep(5.0)

    def _record_decision_event(
        self,
        symbol: str,
        reason: str,
        telemetry: dict[str, Any],
        source: str = "microstructure",
        model: str = "heuristic_microstructure",
    ) -> None:
        """Log decision event to SQLite exit_evaluations table for machine learning."""
        try:
            record = {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol,
                "position_shares": telemetry.get("shares", 0),
                "avg_cost": telemetry.get("avg_cost", 0),
                "price_t0": telemetry.get("current_price", 0),
                "unrealized_pl": (telemetry.get("current_price", 0) - telemetry.get("avg_cost", 0)) * telemetry.get("shares", 0),
                "return_pct": telemetry.get("return_pct", 0),
                "hold_minutes": 0,
                "features": telemetry,
                "predicted_action": "EXIT",
                "confidence": 0.90 if source == "microstructure" else 0.80,
                "signal_type": reason,
                "reason": f"[{source.upper()}] {reason}",
                "model": model,
                "provider": "local",
            }
            save_exit_evaluation(record)
        except Exception as exc:
            logging.debug("Could not record exit evaluation to sqlite: %s", exc)
