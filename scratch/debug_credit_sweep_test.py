import sys
import os
from datetime import datetime
import pandas as pd
from pathlib import Path
from unittest.mock import patch

# Add project root to sys.path
sys.path.append(str(Path(__file__).parent.parent))

import credit_sweep
import config
from tests.test_credit_sweep import bullish_frame, bearish_frame

def debug_bullish():
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
        print("Signal confirmed:", signal.confirmed)
        print("Reject reason:", signal.reject_reason)
        print("Signal direction:", signal.direction)

if __name__ == "__main__":
    debug_bullish()
