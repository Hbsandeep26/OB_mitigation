import os
import sys
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import credit_sweep


def candle(ts, open_, high, low, close, volume=1000):
    return {
        "dt": pd.Timestamp(ts),
        "datetime": pd.Timestamp(ts),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "oi": 0,
    }


def bullish_frame():
    rows = [
        candle("2026-06-20 09:15", 100, 104, 99, 102),
        candle("2026-06-20 09:20", 102, 105, 98, 104),
        candle("2026-06-20 15:25", 104, 106, 95, 100),
        candle("2026-06-21 09:15", 100, 101, 99, 100),
        candle("2026-06-21 09:20", 100, 101, 99, 100),
        candle("2026-06-21 09:25", 100, 101, 99, 100),
        candle("2026-06-21 09:30", 100, 100, 94, 96),
        candle("2026-06-21 09:35", 96, 98, 95, 97),
        candle("2026-06-21 09:40", 97, 99, 96, 98),
        candle("2026-06-21 09:45", 98, 100, 97, 99),
        candle("2026-06-21 09:50", 99, 100, 98, 99.5),
        candle("2026-06-21 09:55", 99.5, 100, 99, 99.8),
        candle("2026-06-21 10:00", 99.8, 101, 99, 100.5),
        candle("2026-06-21 10:05", 100.5, 102, 100, 101.5),
        candle("2026-06-21 10:10", 101.5, 104, 101, 103.5, volume=3000),
    ]
    return credit_sweep.normalize_candles(pd.DataFrame(rows))


def bearish_frame():
    rows = [
        candle("2026-06-20 09:15", 100, 105, 96, 98),
        candle("2026-06-20 15:25", 98, 106, 95, 100),
        candle("2026-06-21 09:15", 100, 101, 99, 100),
        candle("2026-06-21 09:20", 100, 101, 99, 100),
        candle("2026-06-21 09:25", 100, 101, 99, 100),
        candle("2026-06-21 09:30", 100, 107, 100, 104),
        candle("2026-06-21 09:35", 104, 105, 102, 103),
        candle("2026-06-21 09:40", 103, 104, 101, 102),
        candle("2026-06-21 09:45", 102, 103, 100.5, 101.5),
        candle("2026-06-21 09:50", 101.5, 102, 100.8, 101),
        candle("2026-06-21 09:55", 101, 101.5, 100, 100.5),
        candle("2026-06-21 10:00", 100.5, 101, 99.8, 100),
        candle("2026-06-21 10:05", 100, 100.5, 98.8, 99),
        candle("2026-06-21 10:10", 99, 99.2, 96, 97, volume=3000),
    ]
    return credit_sweep.normalize_candles(pd.DataFrame(rows))


class CreditSweepTests(unittest.TestCase):
    def test_bullish_sweep_bos_signal_is_confirmed(self):
        df = bullish_frame()
        levels = credit_sweep.prior_day_levels(df, "2026-06-21")
        with patch("config.CREDIT_SWEEP_SYMBOLS", ["NIFTY"]), \
             patch("config.CREDIT_SWEEP_MAX_SIGNAL_AGE_SECONDS", 90), \
             patch("config.CREDIT_SWEEP_MIN_SCORE", 75):
            signal = credit_sweep.evaluate_credit_sweep_signal(
                "NIFTY",
                df[df["date"] == "2026-06-21"],
                levels,
                now=datetime(2026, 6, 21, 10, 15, 30),
            )

        self.assertTrue(signal.confirmed, signal.reject_reason)
        self.assertEqual(signal.direction, "BULLISH")
        self.assertGreaterEqual(signal.score, 75)
        self.assertGreater(signal.target_price, signal.entry_price)

    def test_bearish_sweep_bos_signal_is_confirmed(self):
        df = bearish_frame()
        levels = credit_sweep.prior_day_levels(df, "2026-06-21")
        with patch("config.CREDIT_SWEEP_SYMBOLS", ["NIFTY"]), \
             patch("config.CREDIT_SWEEP_MIN_SCORE", 75):
            signal = credit_sweep.evaluate_credit_sweep_signal(
                "NIFTY",
                df[df["date"] == "2026-06-21"],
                levels,
                now=datetime(2026, 6, 21, 10, 15, 30),
            )

        self.assertTrue(signal.confirmed, signal.reject_reason)
        self.assertEqual(signal.direction, "BEARISH")
        self.assertLess(signal.target_price, signal.entry_price)

    def test_rejects_stale_signal_candle(self):
        df = bullish_frame()
        levels = credit_sweep.prior_day_levels(df, "2026-06-21")
        with patch("config.CREDIT_SWEEP_SYMBOLS", ["NIFTY"]), \
             patch("config.CREDIT_SWEEP_MAX_SIGNAL_AGE_SECONDS", 90):
            signal = credit_sweep.evaluate_credit_sweep_signal(
                "NIFTY",
                df[df["date"] == "2026-06-21"],
                levels,
                now=datetime(2026, 6, 21, 10, 17, 0),
            )

        self.assertFalse(signal.confirmed)
        self.assertIn("stale", signal.reject_reason)

    def test_rejects_late_day_signal_window(self):
        df = bullish_frame()
        levels = credit_sweep.prior_day_levels(df, "2026-06-21")
        with patch("config.CREDIT_SWEEP_SYMBOLS", ["NIFTY"]), \
             patch("config.CREDIT_SWEEP_ENTRY_CUTOFF", "10:00"):
            signal = credit_sweep.evaluate_credit_sweep_signal(
                "NIFTY",
                df[df["date"] == "2026-06-21"],
                levels,
                now=datetime(2026, 6, 21, 10, 15, 30),
            )

        self.assertFalse(signal.confirmed)
        self.assertIn("outside", signal.reject_reason)

    def test_price_distance_rejects_near_target_and_stop(self):
        signal = credit_sweep.CreditSweepSignal(
            symbol="NIFTY",
            direction="BULLISH",
            status="CONFIRMED",
            entry_price=100.0,
            stop_price=90.0,
            target_price=110.0,
            atr14=2.0,
        )

        near_target, target_reason = credit_sweep.validate_live_price_distance(signal, 109.5)
        near_stop, stop_reason = credit_sweep.validate_live_price_distance(signal, 91.0)

        self.assertFalse(near_target)
        self.assertIn("target", target_reason)
        self.assertFalse(near_stop)
        self.assertIn("stop", stop_reason)

    def test_credit_spread_route_requires_credit_and_risk_budget(self):
        invalid_credit = SimpleNamespace(
            no_trade_reason="",
            entry_prices={"buy_pe": 5.0, "sell_pe": 4.0},
            strikes={"buy_pe": 90.0, "sell_pe": 100.0},
            order_sequence=[("buy_pe", "BUY"), ("sell_pe", "SELL")],
        )
        ok, reason, _ = credit_sweep.validate_credit_spread_route(invalid_credit)
        self.assertFalse(ok)
        self.assertIn("not a real credit", reason)

        over_budget = SimpleNamespace(
            no_trade_reason="",
            entry_prices={"buy_pe": 1.0, "sell_pe": 2.0},
            strikes={"buy_pe": 90.0, "sell_pe": 500.0},
            order_sequence=[("buy_pe", "BUY"), ("sell_pe", "SELL")],
        )
        ok, reason, metrics = credit_sweep.validate_credit_spread_route(over_budget, risk_budget=300.0, quantity=1)
        self.assertFalse(ok)
        self.assertGreater(metrics["defined_loss"], 300.0)
        self.assertIn("exceeds risk budget", reason)

    def test_paper_entry_only_logs_simulated_position(self):
        signal = credit_sweep.CreditSweepSignal(
            symbol="NIFTY",
            direction="BULLISH",
            status="CONFIRMED",
            entry_price=100.0,
            stop_price=90.0,
            target_price=110.0,
            risk_points=10.0,
            reward_points=10.0,
            score=90,
            notes=["BOS"],
        )
        route = SimpleNamespace(
            legs={"buy_pe": "BPE", "sell_pe": "SPE"},
            entry_prices={"buy_pe": 1.0, "sell_pe": 2.0},
            strikes={"buy_pe": 90.0, "sell_pe": 100.0},
            order_sequence=[("buy_pe", "BUY"), ("sell_pe", "SELL")],
        )

        with patch("credit_sweep.log_trade") as log_trade:
            position = credit_sweep.record_paper_entry(
                signal,
                route,
                {"net_credit": 1.0, "spread_width": 10.0, "defined_loss": 9.0},
                fresh_spot=100.2,
            )

        self.assertTrue(position["paper_only"])
        self.assertEqual(position["strategy_type"], credit_sweep.STRATEGY_BULL_PUT)
        log_trade.assert_called_once()


if __name__ == "__main__":
    unittest.main()
