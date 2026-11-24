import unittest

import pandas as pd

from strategy_57pct import add_features, run, strategy_signal
from scripts.app_minute_interval_sweep import promotion_gate


class GridStrategyTests(unittest.TestCase):
    def candles(self):
        index = pd.date_range("2026-01-01", periods=12, freq="10min")
        rows = []
        for i in range(len(index)):
            opening = 90.0
            if i == 7:
                opening = 101.0
            elif i == 9:
                opening = 79.0
            rows.append({"open": opening, "high": max(101.0, opening), "low": min(79.0, opening), "close": opening, "volume": 1.0})
        return pd.DataFrame(rows, index=index)

    def test_features_are_grid_only(self):
        frame = add_features(self.candles(), {"grid_lookback": 3, "grid_intervals": 4})
        for name in ("open_line", "close_line", "grid_top", "grid_upper_mid", "grid_lower_mid", "grid_bottom", "buy_signal", "close_signal"):
            self.assertIn(name, frame)
        for legacy in ("ema_fast", "ema_slow", "atr_exit", "tp_line", "sl_line"):
            self.assertNotIn(legacy, frame)

    def test_down_cross_closes_long(self):
        _, trades, _ = run(self.candles(), {"grid_lookback": 3, "grid_intervals": 4}, slippage_bps=0)
        self.assertGreaterEqual(len(trades), 1)
        self.assertIn("OPEN_CROSSED_GRID_DOWN", {trade.reason for trade in trades})

    def test_live_signal_uses_latest_completed_bar(self):
        frame = self.candles()
        signal = strategy_signal(frame, {"grid_lookback": 3, "grid_intervals": 4})
        self.assertEqual(signal["bar"], str(frame.index[-1]))

    def test_promotion_gate_is_two_sessions_and_two_trades(self):
        winner = {"evidence": "NORMAL", "best": {"stats": {"trades": 2}}}
        self.assertEqual(promotion_gate(winner, 2), (True, "minimum promotion gate passed"))
        self.assertFalse(promotion_gate(winner, 1)[0])
        self.assertFalse(promotion_gate({"evidence": "WEAK", "best": {"stats": {"trades": 2}}}, 2)[0])
        self.assertFalse(promotion_gate({"evidence": "NORMAL", "best": {"stats": {"trades": 1}}}, 2)[0])


if __name__ == "__main__":
    unittest.main()
