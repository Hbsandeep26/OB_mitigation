import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config
from backtest_orderblock_mitigation import validate_mtf_signal


class TestMTFSignalHardening(unittest.TestCase):
    def setUp(self):
        # Setup defaults matching config defaults
        self.symbol = "NIFTY"
        self.time_str = "10:30"
        self.score = 95
        self.trend = 1  # BULLISH
        self.curr_price = 22000.0
        self.stop_loss = 21950.0
        self.target = 22150.0
        self.atr_1m = 20.0
        self.pd_low = 21960.0
        self.pd_high = 22080.0
        self.ob_low = 21955.0
        self.ob_high = 22020.0
        self.zone_entry = 21980.0
        
        # Fresh spot confirms direction
        self.fresh_spot = 22002.0  # Drift is 2.0 (<= 0.25 * 20 ATR = 5.0)

    @patch("config.MTF_VALIDATED_SYMBOLS", ["NIFTY", "SENSEX", "RELIANCE"])
    @patch("config.MTF_ENTRY_START", "09:30")
    @patch("config.MTF_ENTRY_CUTOFF", "13:45")
    @patch("config.MTF_MIN_SCORE", 90)
    @patch("config.MTF_MIN_LIVE_RR", 1.75)
    @patch("config.MTF_MAX_SIGNAL_AGE_SECONDS", 75)
    @patch("config.MTF_MAX_ENTRY_DRIFT_ATR", 0.25)
    @patch("config.MTF_SLIPPAGE_BUFFER_BPS", 3.0)
    def test_clean_synthetic_setup_accepted(self):
        is_valid, reason, rr = validate_mtf_signal(
            symbol=self.symbol,
            time_str=self.time_str,
            score=self.score,
            trend=self.trend,
            curr_price=self.curr_price,
            stop_loss=self.stop_loss,
            target=self.target,
            atr_1m=self.atr_1m,
            pd_low=self.pd_low,
            pd_high=self.pd_high,
            ob_low=self.ob_low,
            ob_high=self.ob_high,
            zone_entry=self.zone_entry,
            is_live=False,
            fresh_spot=self.fresh_spot,
        )
        self.assertTrue(is_valid, f"Clean setup was rejected: {reason}")
        self.assertGreaterEqual(rr, 1.75)

    @patch("config.MTF_VALIDATED_SYMBOLS", ["NIFTY", "SENSEX", "RELIANCE"])
    @patch("config.MTF_ENTRY_START", "09:30")
    @patch("config.MTF_ENTRY_CUTOFF", "13:45")
    def test_reject_unvalidated_symbol(self):
        is_valid, reason, _ = validate_mtf_signal(
            symbol="SBIN",
            time_str=self.time_str,
            score=self.score,
            trend=self.trend,
            curr_price=self.curr_price,
            stop_loss=self.stop_loss,
            target=self.target,
            atr_1m=self.atr_1m,
            pd_low=self.pd_low,
            pd_high=self.pd_high,
            ob_low=self.ob_low,
            ob_high=self.ob_high,
            zone_entry=self.zone_entry,
            is_live=False,
            fresh_spot=self.fresh_spot,
        )
        self.assertFalse(is_valid)
        self.assertIn("not in validated universe", reason)

    @patch("config.MTF_VALIDATED_SYMBOLS", ["NIFTY", "SENSEX", "RELIANCE"])
    @patch("config.MTF_ENTRY_START", "09:30")
    @patch("config.MTF_ENTRY_CUTOFF", "13:45")
    def test_reject_outside_time_window(self):
        # 15:29 Axisbank style
        is_valid, reason, _ = validate_mtf_signal(
            symbol=self.symbol,
            time_str="15:29",
            score=self.score,
            trend=self.trend,
            curr_price=self.curr_price,
            stop_loss=self.stop_loss,
            target=self.target,
            atr_1m=self.atr_1m,
            pd_low=self.pd_low,
            pd_high=self.pd_high,
            ob_low=self.ob_low,
            ob_high=self.ob_high,
            zone_entry=self.zone_entry,
            is_live=False,
            fresh_spot=self.fresh_spot,
        )
        self.assertFalse(is_valid)
        self.assertIn("outside window", reason)

    @patch("config.MTF_VALIDATED_SYMBOLS", ["NIFTY", "SENSEX", "RELIANCE"])
    @patch("config.MTF_MIN_SCORE", 90)
    def test_reject_low_score(self):
        is_valid, reason, _ = validate_mtf_signal(
            symbol=self.symbol,
            time_str=self.time_str,
            score=85,
            trend=self.trend,
            curr_price=self.curr_price,
            stop_loss=self.stop_loss,
            target=self.target,
            atr_1m=self.atr_1m,
            pd_low=self.pd_low,
            pd_high=self.pd_high,
            ob_low=self.ob_low,
            ob_high=self.ob_high,
            zone_entry=self.zone_entry,
            is_live=False,
            fresh_spot=self.fresh_spot,
        )
        self.assertFalse(is_valid)
        self.assertIn("Score", reason)

    @patch("config.MTF_VALIDATED_SYMBOLS", ["NIFTY", "SENSEX", "RELIANCE"])
    @patch("config.MTF_MAX_SIGNAL_AGE_SECONDS", 75)
    def test_reject_stale_candle(self):
        signal_dt = datetime(2026, 6, 21, 10, 0, 0)
        # Candle closes at 10:01:00. If now is 10:02:20, age is 80 seconds.
        now = datetime(2026, 6, 21, 10, 2, 20)
        is_valid, reason, _ = validate_mtf_signal(
            symbol=self.symbol,
            time_str=self.time_str,
            score=self.score,
            trend=self.trend,
            curr_price=self.curr_price,
            stop_loss=self.stop_loss,
            target=self.target,
            atr_1m=self.atr_1m,
            pd_low=self.pd_low,
            pd_high=self.pd_high,
            ob_low=self.ob_low,
            ob_high=self.ob_high,
            zone_entry=self.zone_entry,
            is_live=True,
            signal_dt=signal_dt,
            now=now,
            fresh_spot=self.fresh_spot,
        )
        self.assertFalse(is_valid)
        self.assertIn("stale", reason)

    @patch("config.MTF_VALIDATED_SYMBOLS", ["NIFTY", "SENSEX", "RELIANCE"])
    def test_reject_direction_violation(self):
        # Bullish setup but price drops below ob_low
        is_valid, reason, _ = validate_mtf_signal(
            symbol=self.symbol,
            time_str=self.time_str,
            score=self.score,
            trend=self.trend,
            curr_price=self.curr_price,
            stop_loss=self.stop_loss,
            target=self.target,
            atr_1m=self.atr_1m,
            pd_low=self.pd_low,
            pd_high=self.pd_high,
            ob_low=self.ob_low,
            ob_high=self.ob_high,
            zone_entry=self.zone_entry,
            is_live=False,
            fresh_spot=21952.0,  # Below ob_low (21955.0)
        )
        self.assertFalse(is_valid)
        self.assertIn("violates bullish direction", reason)

    @patch("config.MTF_VALIDATED_SYMBOLS", ["NIFTY", "SENSEX", "RELIANCE"])
    @patch("config.MTF_MAX_ENTRY_DRIFT_ATR", 0.25)
    def test_reject_excessive_drift(self):
        is_valid, reason, _ = validate_mtf_signal(
            symbol=self.symbol,
            time_str=self.time_str,
            score=self.score,
            trend=self.trend,
            curr_price=self.curr_price,
            stop_loss=self.stop_loss,
            target=self.target,
            atr_1m=self.atr_1m,
            pd_low=self.pd_low,
            pd_high=self.pd_high,
            ob_low=self.ob_low,
            ob_high=self.ob_high,
            zone_entry=self.zone_entry,
            is_live=False,
            # Drift is 6.0 (> 0.25 * 20.0 ATR = 5.0)
            fresh_spot=self.curr_price + 6.0,
        )
        self.assertFalse(is_valid)
        self.assertIn("Drift", reason)

    @patch("config.MTF_VALIDATED_SYMBOLS", ["NIFTY", "SENSEX", "RELIANCE"])
    def test_reject_target_near_miss_or_touched(self):
        # Bullish setup but spot is near target 22150.0 (zone entry 21980.0, total dist 170.0)
        # 10% remaining is 17.0 points, so reject if spot >= 22133.0
        is_valid, reason, _ = validate_mtf_signal(
            symbol=self.symbol,
            time_str=self.time_str,
            score=self.score,
            trend=self.trend,
            curr_price=22132.0,
            stop_loss=self.stop_loss,
            target=self.target,
            atr_1m=self.atr_1m,
            pd_low=self.pd_low,
            pd_high=self.pd_high,
            ob_low=self.ob_low,
            ob_high=self.ob_high,
            zone_entry=self.zone_entry,
            is_live=False,
            fresh_spot=22135.0,  # Within 10% (remaining 15 points)
        )
        self.assertFalse(is_valid)
        self.assertIn("Target already reached or near", reason)

    @patch("config.MTF_VALIDATED_SYMBOLS", ["NIFTY", "SENSEX", "RELIANCE"])
    @patch("config.MTF_MIN_LIVE_RR", 1.75)
    @patch("config.MTF_SLIPPAGE_BUFFER_BPS", 3.0)
    def test_reject_poor_rr(self):
        # Target 22050 (only 50 pts away), risk is 50 pts
        # With 3 BPS slippage on 22000 spot (= 6.6 pts entry offset), RR becomes 43.4 / 56.6 = 0.77R (< 1.75R)
        is_valid, reason, _ = validate_mtf_signal(
            symbol=self.symbol,
            time_str=self.time_str,
            score=self.score,
            trend=self.trend,
            curr_price=self.curr_price,
            stop_loss=self.stop_loss,
            target=22050.0,
            atr_1m=self.atr_1m,
            pd_low=self.pd_low,
            pd_high=self.pd_high,
            ob_low=self.ob_low,
            ob_high=self.ob_high,
            zone_entry=self.zone_entry,
            is_live=False,
            fresh_spot=self.fresh_spot,
        )
        self.assertFalse(is_valid)
        self.assertIn("Remaining RR", reason)


if __name__ == "__main__":
    unittest.main()
