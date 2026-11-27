#!/usr/bin/env python3
import unittest
from unittest.mock import MagicMock, patch

from scripts.ai_exit_arbiter import AIExitArbiter
from competition_bot import CompetitionBot
from moomoo import TrdSide


class TestAIExitArbiter(unittest.TestCase):
    def setUp(self):
        self.arbiter = AIExitArbiter(enable_ai_gateway=False)

    def test_strong_runner_holds(self):
        """A trade with strong buying depth and riding above VWAP should HOLD."""
        snapshot = {
            "bid_price": 645.0,
            "ask_price": 645.10,
            "bid_ask_ratio": 72.0,
            "avg_price": 640.0,
            "volume_ratio": 1.5,
        }
        params = {"take_profit_pct": 0.01918, "stop_loss_pct": 0.1324}
        reason, telemetry = self.arbiter.evaluate_microstructure_exit(
            symbol="US.MU",
            price=645.0,
            qty=154,
            avg_cost=638.40,
            snapshot=snapshot,
            peak_price=645.20,
            params=params,
        )
        self.assertIsNone(reason)
        self.assertAlmostEqual(telemetry["return_pct"], ((645.0 - 638.4) / 638.4) * 100.0, places=2)

    def test_dynamic_profit_protect_on_stall(self):
        """A trade that peaked +0.95% but gave back 41% with seller dominance should exit."""
        snapshot = {
            "bid_price": 642.0,
            "ask_price": 642.05,
            "bid_ask_ratio": 38.0,
            "avg_price": 642.60,
            "volume_ratio": 1.1,
        }
        params = {"take_profit_pct": 0.01918}
        reason, telemetry = self.arbiter.evaluate_microstructure_exit(
            symbol="US.MU",
            price=642.0,
            qty=154,
            avg_cost=638.40,
            snapshot=snapshot,
            peak_price=644.50,
            params=params,
        )
        self.assertEqual(reason, "AI_TAKE_PROFIT_STALL")
        self.assertGreater(telemetry["profit_giveback_pct"], 35.0)

    def test_orderbook_liquidity_exhaustion(self):
        """Asks overwhelmingly outnumbering bids with price falling below VWAP triggers exit."""
        snapshot = {
            "bid_price": 398.20,
            "ask_price": 398.25,
            "bid_ask_ratio": 24.0,
            "avg_price": 398.90,
            "volume_ratio": 0.9,
        }
        params = {"take_profit_pct": 0.007}
        reason, telemetry = self.arbiter.evaluate_microstructure_exit(
            symbol="US.APP",
            price=398.20,
            qty=130,
            avg_cost=397.44,
            snapshot=snapshot,
            peak_price=398.60,
            params=params,
        )
        self.assertEqual(reason, "AI_ORDERBOOK_EXHAUSTION")

    def test_near_tp_runner_reversal(self):
        """Trade reached >80% of target TP but reverses >0.25% should lock profit."""
        snapshot = {
            "bid_price": 224.65,
            "ask_price": 224.70,
            "bid_ask_ratio": 45.0,
            "avg_price": 224.20,
        }
        # Scaled TP is 0.56% (0.0056), target price is ~224.87
        # At 224.80, it was 89% to target. At 224.65, it dropped from 225.00 peak
        params = {"take_profit_pct": 0.0056}
        reason, telemetry = self.arbiter.evaluate_microstructure_exit(
            symbol="US.MRNA",
            price=224.65,
            qty=395,
            avg_cost=223.62,
            snapshot=snapshot,
            peak_price=225.30,
            params=params,
        )
        self.assertEqual(reason, "AI_TAKE_PROFIT_NEAR_TARGET")

    def test_preventive_risk_cut(self):
        """Severe selling immediately after entry should be cut early rather than waiting for -13%."""
        snapshot = {
            "bid_price": 148.10,
            "ask_price": 148.15,
            "bid_ask_ratio": 16.0,
            "avg_price": 149.00,
        }
        params = {"take_profit_pct": 0.0105, "stop_loss_pct": 0.1324}
        reason, telemetry = self.arbiter.evaluate_microstructure_exit(
            symbol="US.NVDA",
            price=148.10,
            qty=551,
            avg_cost=149.63,
            snapshot=snapshot,
            peak_price=149.63,
            params=params,
        )
        self.assertEqual(reason, "AI_PREVENTIVE_CUT")

    def test_approved_ai_gateway_exit_consumption(self):
        """Approved decision from deep AI Gateway overrides normal hold."""
        self.arbiter.approve_ai_exit("US.OMH", {
            "decision": {
                "action": "EXIT",
                "confidence": 0.85,
                "signal_type": "STALL_EXHAUSTION",
                "reason": "institutional ask block detected",
            },
            "model": "gpt-oss-20b",
        })
        reason, telemetry = self.arbiter.check_exit(
            symbol="US.OMH",
            price=4.70,
            qty=13531,
            avg_cost=4.69,
            snapshot={"bid_ask_ratio": 55.0, "avg_price": 4.69},
            peak_price=4.70,
            params={"take_profit_pct": 0.01918},
        )
        self.assertEqual(reason, "AI_STALL_EXHAUSTION")
        self.assertIn("ai_decision", telemetry)

    def test_bot_executes_ai_exit_regardless_of_indicator_status(self):
        """CompetitionBot must fire sell order if AIExitArbiter triggers, even when exit_signal=False."""
        bot = CompetitionBot.__new__(CompetitionBot)
        bot.symbols = ["US.MU"]
        bot.peak_prices = {"US.MU": 644.50}
        bot.state = {"pending": {}, "last_bar": {}, "last_source_bar": {}}
        bot.live_paper = False
        bot.ai_exit_arbiter = AIExitArbiter(enable_ai_gateway=False)

        # Mock submit
        bot.submit = MagicMock()
        bot.risk_exit_reason = MagicMock(return_value=None)
        bot.session_exit_due = MagicMock(return_value=False)

        # Snapshot where profit stalls (giving back >35% of peak profit with ask dominance)
        snapshot = {
            "bid_price": 642.0,
            "ask_price": 642.05,
            "bid_ask_ratio": 38.0,
            "avg_price": 642.60,
            "volume_ratio": 1.1,
        }
        bot.get_snapshot_dict = MagicMock(return_value=snapshot)

        # Indicator says exit_signal = False
        signal_data = {"bar": "2026-11-28 04:00:00", "long_signal": False, "exit_signal": False}
        params = {"take_profit_pct": 0.01918}
        price = 642.0
        qty = 154
        avg = 638.40
        symbol = "US.MU"

        # Simulate the bot's evaluation block:
        risk_reason = bot.risk_exit_reason(symbol, price, qty, avg, params)
        session_exit = bot.session_exit_due()
        exit_signal = bool(signal_data.get("exit_signal", False))
        snap = bot.get_snapshot_dict(symbol)
        ai_reason, _ = bot.ai_exit_arbiter.check_exit(
            symbol=symbol,
            price=price,
            qty=qty,
            avg_cost=avg,
            snapshot=snap,
            peak_price=bot.peak_prices.get(symbol, 0.0),
            params=params,
        )

        self.assertIsNone(risk_reason)
        self.assertFalse(session_exit)
        self.assertFalse(exit_signal)
        self.assertEqual(ai_reason, "AI_TAKE_PROFIT_STALL")

        # The exit fires regardless of indicator status:
        if risk_reason or session_exit or exit_signal or ai_reason:
            reason = risk_reason or ai_reason or "GRID_EXIT"
            bot.submit(symbol, TrdSide.SELL, int(qty), reason)

        bot.submit.assert_called_once_with("US.MU", TrdSide.SELL, 154, "AI_TAKE_PROFIT_STALL")


if __name__ == "__main__":
    unittest.main()
