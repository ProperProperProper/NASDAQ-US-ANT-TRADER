import unittest
import json
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from strategy_57pct import add_features, run, strategy_signal
from minute_bars import MINUTE_INTERVALS, aggregate_us_session
from scripts.app_minute_interval_sweep import parse_intervals, promotion_gate, promote_winner
from scripts.research_scheduler import session_phase
from competition_bot import CompetitionBot, MAX_OPEN_POSITIONS, count_open_positions
from trading_config import active_symbol, normalize_symbol, strategy_path


ET = ZoneInfo("America/New_York")


class GridStrategyTests(unittest.TestCase):
    def test_active_symbol_configuration_and_paths_are_isolated(self):
        self.assertEqual(active_symbol(), "US.ANT")
        self.assertEqual(normalize_symbol("aapl"), "US.AAPL")
        self.assertNotEqual(strategy_path("US.ANT"), strategy_path("US.AAPL"))
        with self.assertRaises(ValueError):
            normalize_symbol("AU.BHP")

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

    def test_trailing_stop_is_supported(self):
        index = pd.date_range("2026-01-01", periods=12, freq="10min")
        openings = [90, 90, 90, 90, 90, 90, 90, 101, 102, 101.4, 101.5, 101.5]
        candles = pd.DataFrame(
            [{"open": value, "high": value, "low": value, "close": value, "volume": 1.0} for value in openings],
            index=index,
        )
        params = {"grid_lookback": 3, "grid_intervals": 4, "trailing_activation_pct": 0.009, "trailing_stop_pct": 0.005}
        _, trades, _ = run(candles, params, slippage_bps=0)
        self.assertTrue(any(trade.reason == "TRAILING_STOP" for trade in trades))

    def test_live_risk_exits_are_evaluated_intrabar(self):
        bot = CompetitionBot.__new__(CompetitionBot)
        bot.peak_price = 0.0
        params = {"take_profit_pct": 0.01, "stop_loss_pct": 0.0, "trailing_activation_pct": 0.0, "trailing_stop_pct": 0.0}
        self.assertEqual(bot.risk_exit_reason(101.0, 100, 100.0, params), "TAKE_PROFIT")
        bot.peak_price = 100.0
        trailing = {"take_profit_pct": 0.0, "stop_loss_pct": 0.0, "trailing_activation_pct": 0.05, "trailing_stop_pct": 0.01}
        self.assertEqual(bot.risk_exit_reason(98.0, 100, 90.0, trailing), "TRAILING_STOP")

    def test_session_exit_persists_no_reentry_lock(self):
        bot = object.__new__(CompetitionBot)
        bot.state = {"session_exit_date": {"US.ANT": datetime.now(ET).date().isoformat()}}
        self.assertTrue(bot.session_reentry_blocked("US.ANT"))

    def test_backtest_exits_thirty_minutes_before_close(self):
        index = pd.date_range("2026-01-05 15:20", periods=12, freq="1min")
        openings = [90, 90, 90, 90, 90, 90, 90, 101, 101, 101, 101, 101]
        candles = pd.DataFrame(
            [{"open": value, "high": max(101, value), "low": min(79, value), "close": value, "volume": 1.0} for value in openings],
            index=index,
        )
        _, trades, _ = run(candles, {"grid_lookback": 3, "grid_intervals": 4}, slippage_bps=0)
        self.assertEqual(trades[-1].reason, "SESSION_EXIT_30M_BEFORE_CLOSE")

    def test_live_signal_uses_latest_completed_bar(self):
        frame = self.candles()
        signal = strategy_signal(frame, {"grid_lookback": 3, "grid_intervals": 4})
        self.assertEqual(signal["bar"], str(frame.index[-1]))

    def test_live_aggregation_can_retain_latest_partial_bucket(self):
        index = pd.date_range("2026-01-05 09:30", periods=5, freq="1min")
        frame = pd.DataFrame({"open": range(5), "high": range(5), "low": range(5), "close": range(5), "volume": 1.0}, index=index)
        complete = aggregate_us_session(frame, 3)
        live = aggregate_us_session(frame, 3, include_incomplete=True)
        self.assertEqual(len(complete), 1)
        self.assertEqual(len(live), 2)

    def test_paper_and_research_intervals_are_one_to_ten_minutes(self):
        self.assertEqual(MINUTE_INTERVALS, tuple(range(1, 11)))
        self.assertEqual(parse_intervals(None), list(MINUTE_INTERVALS))
        with self.assertRaises(ValueError):
            parse_intervals("11")
        with self.assertRaises(ValueError):
            aggregate_us_session(pd.DataFrame(), 11)

    def test_promotion_gate_requires_profit_and_beats_current(self):
        winner = {"best": {"stats": {"trades": 2, "total_return_pct": 2.0}}}
        self.assertFalse(promotion_gate(winner, completed_sessions=2)[0])
        self.assertFalse(promotion_gate(winner, completed_sessions=1)[0])
        self.assertFalse(promotion_gate({"best": {"stats": {"trades": 1, "total_return_pct": 2.0}}}, completed_sessions=2)[0])
        self.assertFalse(promotion_gate({"best": {"stats": {"trades": 2, "total_return_pct": 0.0}}}, completed_sessions=2)[0])
        self.assertFalse(promotion_gate(winner, {"total_return_pct": 2.0}, 2)[0])
        self.assertTrue(promotion_gate(winner, {"total_return_pct": 1.0}, 2)[0])

    def test_eligible_promotion_is_queued_for_broker_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pending = root / "pending_paper_strategy_promotion.json"
            with patch("scripts.app_minute_interval_sweep.PENDING_PROMOTION_PATH", pending):
                result = promote_winner({"interval": "8m", "params": {"grid_lookback": 52}, "run_id": "test"})
            self.assertEqual(result["state"], "QUEUED_BROKER_CHECK")
            payload = json.loads(pending.read_text())
            self.assertEqual((payload["symbol"], payload["interval"], payload["mode"]), ("US.ANT", 8, "SIMULATE"))
            self.assertFalse((root / "paper_bot_live_strategy.json").exists())

    def test_research_phases_use_us_eastern_market_hours(self):
        self.assertEqual(session_phase(pd.Timestamp("2026-01-05 12:00", tz="UTC").to_pydatetime()), "PREMARKET")
        self.assertEqual(session_phase(pd.Timestamp("2026-01-05 15:00", tz="UTC").to_pydatetime()), "RTH")
        self.assertEqual(session_phase(pd.Timestamp("2026-01-05 22:00", tz="UTC").to_pydatetime()), "CLOSED")

    def test_open_position_limit_counts_positive_holdings(self):
        positions = pd.DataFrame({"code": ["US.ANT", "US.X", "US.Y", "US.Z"], "qty": [10, 0, 3, 1]})
        self.assertEqual(MAX_OPEN_POSITIONS, 1)
        self.assertEqual(count_open_positions(positions), 3)

    def test_open_position_limit_counts_short_holdings(self):
        positions = pd.DataFrame({"code": ["US.ANT", "US.X"], "qty": [-6378, 0]})
        self.assertEqual(count_open_positions(positions), 1)


if __name__ == "__main__":
    unittest.main()
