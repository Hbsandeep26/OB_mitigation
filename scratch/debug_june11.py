import sys
import os
sys.path.append(r"c:\Users\sande\Antigravity_upstox_selling\Dhan_Algo")

import pandas as pd
from pathlib import Path
from backtest_orderblock_mitigation import BacktestParams, load_candles, run_5m_ob_tracker

def main():
    params = BacktestParams()
    df_5m = load_candles(Path("data/historical/NIFTY_5m.csv"))
    print("5m candles loaded. Total rows:", len(df_5m))
    
    df_state = run_5m_ob_tracker(df_5m, params)
    df_5m["trend"] = df_state["trend"]
    df_5m["ob_low"] = df_state["ob_low"]
    df_5m["ob_high"] = df_state["ob_high"]
    df_5m["zone_entry"] = df_state["zone_entry_price"]
    df_5m["stop_loss_5m"] = df_state["stop_loss_5m"]
    df_5m["take_profit_5m"] = df_state["take_profit_5m"]
    
    june11_5m = df_5m[df_5m["date"] == "2026-06-11"]
    cols = ["time", "open", "high", "low", "close", "trend", "ob_low", "ob_high", "zone_entry", "stop_loss_5m", "take_profit_5m"]
    print("\n5m data for June 11, 2026:")
    print(june11_5m[cols].to_string(index=False))

if __name__ == "__main__":
    main()
