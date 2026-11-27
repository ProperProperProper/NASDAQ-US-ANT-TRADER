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


if __name__ == "__main__":
    unittest.main()
