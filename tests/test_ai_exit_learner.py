#!/usr/bin/env python3
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from scripts.ai_exit_learner import (
    extract_position_features,
    resolve_counterfactual_outcomes,
    calibrate_confidence_threshold,
    evaluate_exit,
)
from quant_storage import save_exit_evaluation, load_historical_analogues


class TestAIExitLearner(unittest.TestCase):
    def test_extract_position_features(self):
        snapshot = {
            "bid_price": 149.10,
            "ask_price": 149.12,
            "bid_ask_ratio": 65.5,
            "volume_ratio": 1.25,
            "amplitude": 3.4,
            "turnover_rate": 0.45,
            "avg_price": 148.80,
        }
        params = {"take_profit_pct": 0.007, "stop_loss_pct": 0.1324}
        features = extract_position_features(
            symbol="US.NVDA",
            shares=551,
            avg_cost=149.63,
            current_price=149.10,
            snapshot=snapshot,
            strategy_params=params,
            hold_minutes=25.0,
            peak_price=150.00,
        )
        self.assertEqual(features["symbol"], "US.NVDA")
        self.assertEqual(features["shares"], 551)
        self.assertAlmostEqual(features["avg_cost"], 149.63, places=2)
        self.assertAlmostEqual(features["return_pct"], ((149.10 - 149.63) / 149.63) * 100.0, places=2)
        self.assertEqual(features["bid_ask_ratio"], 65.5)
        self.assertEqual(features["hold_minutes"], 25.0)

    def test_save_and_load_analogues(self):
        eval_id = save_exit_evaluation({
            "created_at": datetime.now(timezone.utc).isoformat(),
            "symbol": "US.NVDA",
            "position_shares": 100,
            "avg_cost": 150.0,
            "price_t0": 148.0,
            "unrealized_pl": -200.0,
            "return_pct": -1.33,
            "hold_minutes": 30.0,
            "features": {"test": 1},
            "predicted_action": "EXIT",
            "confidence": 0.85,
            "signal_type": "PREVENTIVE_CUT",
            "reason": "Test exit",
            "model": "test-model",
            "provider": "local",
        })
        self.assertGreater(eval_id, 0)

        # Before resolving, analogues with resolved_at are empty
        analogues = load_historical_analogues("US.NVDA", limit=5)
        # Should be a list
        self.assertIsInstance(analogues, list)

    def test_extract_position_features_with_convergence(self):
        import pandas as pd
        closes = [100.0, 100.4, 100.8, 101.5, 101.8, 101.4, 100.9]
        highs = [100.2, 100.6, 101.0, 101.7, 102.0, 101.6, 101.1]
        lows = [99.8, 100.2, 100.6, 101.3, 101.6, 101.2, 100.7]
        candles = pd.DataFrame({"close": closes, "high": highs, "low": lows})

        snapshot = {
            "bid_price": 100.90,
            "ask_price": 100.95,
            "bid_ask_ratio": 36.0,
            "avg_price": 101.00,
        }
        params = {"take_profit_pct": 0.02, "stop_loss_pct": 0.1324, "grid_lookback": 10}

        features = extract_position_features(
            symbol="US.MU",
            shares=100,
            avg_cost=100.0,
            current_price=100.90,
            snapshot=snapshot,
            strategy_params=params,
            peak_price=102.0,
            candles=candles,
        )

        self.assertIn("indicator_convergence", features)
        conv = features["indicator_convergence"]
        self.assertIn("fast_slow_ema_gap", conv)
        self.assertIn("ema_decay_pct", conv)
        self.assertIn("ema_gap_velocity", conv)
        self.assertIn("bars_to_grid_collision", conv)
        self.assertIn("profit_giveback_pct", conv)
        self.assertGreater(conv["profit_giveback_pct"], 50.0)

    @patch("scripts.ai_exit_learner.query_gateway_chat")
    def test_evaluate_exit_with_convergence_learning_exemplars(self, mock_chat):
        mock_chat.return_value = {
            "choices": [{"message": {"content": '{"action": "EXIT", "confidence": 0.88, "signal_type": "CONVERGENCE_PROFIT_LOCK", "reason": "momentum collapsed"}'}}],
            "model": "gpt-oss-20b",
        }
        features = {
            "symbol": "US.MU",
            "shares": 100,
            "avg_cost": 100.0,
            "current_price": 100.90,
            "return_pct": 0.90,
            "indicator_convergence": {
                "fast_slow_ema_gap": 0.05,
                "ema_decay_pct": 68.0,
                "ema_gap_velocity": -0.15,
            },
        }
        mock_analogues = [
            {
                "symbol": "US.MU",
                "return_pct": 0.85,
                "hold_minutes": 15,
                "predicted_action": "EXIT",
                "optimal_action": "EXIT",
                "counterfactual_reward": 1.0,
                "reason": "Exited ahead of collapse",
                "features": {"indicator_convergence": {"ema_decay_pct": 72.0}},
            }
        ]
        res = evaluate_exit(features, analogues=mock_analogues)
        self.assertEqual(res["decision"]["action"], "EXIT")
        self.assertEqual(res["decision"]["signal_type"], "CONVERGENCE_PROFIT_LOCK")


if __name__ == "__main__":
    unittest.main()
