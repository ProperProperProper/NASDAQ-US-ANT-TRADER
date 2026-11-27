import unittest
import json
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from strategy_57pct import Trade, add_features, run, stats, strategy_signal, validate_strategy_params
from minute_bars import MINUTE_INTERVALS, aggregate_us_session
from scripts.app_minute_interval_sweep import parse_intervals, promotion_gate, promote_winner
from scripts.research_scheduler import session_phase
from us_market_calendar import early_close_dates, session_close
from competition_bot import CompetitionBot, MAX_OPEN_POSITIONS, count_open_positions
from trading_config import active_symbol, active_symbols, normalize_symbol, strategy_path
from scripts.multi_symbol_backtest import parse_symbols as parse_backtest_symbols


ET = ZoneInfo("America/New_York")


class GridStrategyTests(unittest.TestCase):
    def test_bot_state_prunes_removed_symbols_and_recovers_invalid_json(self):
        bot = CompetitionBot.__new__(CompetitionBot)
        bot.symbols = ["US.ANT"]
        with tempfile.TemporaryDirectory() as directory:
            bot.state_path = Path(directory) / "bot.json"
            bot.state_path.write_text(json.dumps({"last_bar": {"US.ANT": "now", "US.OLD": "old"}, "pending": {"US.OLD": "BUY"}}))
            with patch("competition_bot.LEGACY_STATE_PATH", Path(directory) / "legacy.json"):
                state = bot._load_state()
            self.assertEqual(state["last_bar"], {"US.ANT": "now"})
            self.assertEqual(state["pending"], {})
            bot.state_path.write_text("{truncated")
            with patch("competition_bot.LEGACY_STATE_PATH", Path(directory) / "legacy.json"):
                recovered = bot._load_state()
            self.assertEqual(recovered["last_bar"], {})

    def test_2026_exchange_early_closes_are_supported(self):
        self.assertEqual(session_close(datetime(2026, 7, 2).date()).isoformat(), "13:00:00")
        self.assertEqual(session_close(datetime(2026, 11, 27).date()).isoformat(), "13:00:00")
        self.assertIn(datetime(2026, 12, 24).date(), early_close_dates(2026))

    def test_strategy_parameters_fail_closed(self):
        with self.assertRaises(ValueError):
            validate_strategy_params({"entry_mode": "invented_mode"})
        with self.assertRaises(ValueError):
            validate_strategy_params({"stop_loss_pct": float("nan")})
        with self.assertRaises(ValueError):
            validate_strategy_params({"grid_lookback": 1000})

    def test_stats_use_portfolio_equity_not_compounded_position_returns(self):
        trade = Trade("2026-01-01", "2026-01-01", 100.0, 950, "2026-01-02", 110.0,
                      9500.0, 0.0, 9500.0, 10.0, "TEST")
        equity = pd.DataFrame({"equity": [100000.0, 109500.0]}, index=pd.to_datetime(["2026-01-01", "2026-01-02"]))
        result = stats([trade], {"equity": equity}, 100000.0)
        self.assertEqual(result["ending_equity"], 109500.0)
        self.assertEqual(result["total_return_pct"], 9.5)

    def test_active_symbol_configuration_and_paths_are_isolated(self):
        self.assertEqual(active_symbol(), "US.MRNA")
        self.assertEqual(active_symbols(), ("US.MRNA", "US.ANT", "US.NVDA", "US.MU", "US.SNDK", "US.OMH", "US.APP"))
        self.assertEqual(normalize_symbol("aapl"), "US.AAPL")
        self.assertNotEqual(strategy_path("US.ANT"), strategy_path("US.AAPL"))
        with self.assertRaises(ValueError):
            normalize_symbol("AU.BHP")

    def test_multi_symbol_backtest_normalizes_and_deduplicates(self):
        self.assertEqual(parse_backtest_symbols("ant, US.AAPL,us.ant"), ["US.ANT", "US.AAPL"])

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
        params = {"take_profit_pct": 0.01, "stop_loss_pct": 0.0, "trailing_activation_pct": 0.0, "trailing_stop_pct": 0.0}
        bot.peak_prices = {}
        self.assertEqual(bot.risk_exit_reason("US.AAPL", 101.0, 100, 100.0, params), "TAKE_PROFIT")
        bot.peak_prices = {"US.AAPL": 100.0}
        trailing = {"take_profit_pct": 0.0, "stop_loss_pct": 0.0, "trailing_activation_pct": 0.05, "trailing_stop_pct": 0.01}
        self.assertEqual(bot.risk_exit_reason("US.AAPL", 98.0, 100, 90.0, trailing), "TRAILING_STOP")

    def test_entries_are_blocked_outside_rth_and_exit_window(self):
        with patch("competition_bot.session_phase", return_value="PREMARKET"):
            self.assertFalse(CompetitionBot.entry_window_open())
        fake_932 = datetime(2026, 1, 5, 9, 32, tzinfo=ET)
        fake_940 = datetime(2026, 1, 5, 9, 40, tzinfo=ET)
        fake_1000 = datetime(2026, 1, 5, 10, 0, tzinfo=ET)
        with patch("competition_bot.session_phase", return_value="RTH"), patch("competition_bot.datetime") as mock_dt, patch.object(CompetitionBot, "session_exit_due", return_value=False):
            mock_dt.now.return_value = fake_932
            self.assertFalse(CompetitionBot.entry_window_open(5))
            # Test custom 15m delay: at 9:40 it is blocked, at 10:00 it is open
            mock_dt.now.return_value = fake_940
            self.assertFalse(CompetitionBot.entry_window_open(15))
            self.assertTrue(CompetitionBot.entry_window_open(5))
        with patch("competition_bot.session_phase", return_value="RTH"), patch("competition_bot.datetime") as mock_dt, patch.object(CompetitionBot, "session_exit_due", return_value=False):
            mock_dt.now.return_value = fake_1000
            self.assertTrue(CompetitionBot.entry_window_open(15))
        with patch("competition_bot.session_phase", return_value="RTH"), patch("competition_bot.datetime") as mock_dt, patch.object(CompetitionBot, "session_exit_due", return_value=True):
            mock_dt.now.return_value = fake_1000
            self.assertFalse(CompetitionBot.entry_window_open(15))

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
                result = promote_winner({"interval": "8m", "params": {"grid_lookback": 52, "take_profit_pct": 0.04, "stop_loss_pct": 0.12}, "run_id": "test"})
            self.assertEqual(result["state"], "QUEUED_BROKER_CHECK")
            payload = json.loads(pending.read_text())
            self.assertEqual((payload["symbol"], payload["interval"], payload["mode"]), ("US.MRNA", 8, "SIMULATE"))
            self.assertEqual(payload["params"]["take_profit_pct"], 0.028)
            self.assertEqual(payload["params"]["stop_loss_pct"], 0.12)
            self.assertFalse((root / "paper_bot_live_strategy.json").exists())

    def test_buy_signal_requires_bullish_candle(self):
        c_green = self.candles()
        f_green = add_features(c_green, {"grid_lookback": 3, "grid_intervals": 4})
        self.assertTrue(bool(f_green.iloc[4]["buy_signal"]))

        c_red = self.candles().copy()
        c_red.iloc[7, c_red.columns.get_loc("close")] = 98.0
        f_red = add_features(c_red, {"grid_lookback": 3, "grid_intervals": 4})
        self.assertFalse(bool(f_red.iloc[4]["buy_signal"]))

    def test_research_phases_use_us_eastern_market_hours(self):
        self.assertEqual(session_phase(pd.Timestamp("2026-01-05 12:00", tz="UTC").to_pydatetime()), "PREMARKET")
        self.assertEqual(session_phase(pd.Timestamp("2026-01-05 15:00", tz="UTC").to_pydatetime()), "RTH")
        self.assertEqual(session_phase(pd.Timestamp("2026-01-05 22:00", tz="UTC").to_pydatetime()), "CLOSED")

    def test_open_position_limit_counts_positive_holdings(self):
        positions = pd.DataFrame({"code": ["US.ANT", "US.X", "US.Y", "US.Z"], "qty": [10, 0, 3, 1]})
        self.assertEqual(MAX_OPEN_POSITIONS, 7)
        self.assertEqual(count_open_positions(positions), 3)

    def test_open_position_limit_counts_short_holdings(self):
        positions = pd.DataFrame({"code": ["US.ANT", "US.X"], "qty": [-6378, 0]})
        self.assertEqual(count_open_positions(positions), 1)


if __name__ == "__main__":
    unittest.main()
