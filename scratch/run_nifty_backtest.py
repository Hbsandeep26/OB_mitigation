import sys
import os
sys.path.append(r"c:\Users\sande\Antigravity_upstox_selling\Dhan_Algo")

import pandas as pd
from pathlib import Path
from backtest_orderblock_mitigation import BacktestParams, load_candles, backtest_ob_mitigation

def main():
    params = BacktestParams(
        trigger_type="choch",
        stop_loss_type="5m_origin",
        strategy_type="Ratio",
        use_vwap_filter=True  # Enable VWAP filter
    )
    df_5m = load_candles(Path("data/historical/NIFTY_5m.csv"))
    df_1m = load_candles(Path("data/historical/NIFTY_1m.csv"))
    
    trades = backtest_ob_mitigation("NIFTY", df_5m, df_1m, params)
    
    # Filter trades for June 11 and 12, 2026
    filtered_trades = [t for t in trades if t.date in ("2026-06-11", "2026-06-12")]
    
    print(f"Total trades on June 11 & 12: {len(filtered_trades)}")
    for t in filtered_trades:
        print(f"{t.symbol}\t{t.date}\t{t.direction}\t{t.entry_time}\t{t.exit_time}\t{t.entry_price}\t{t.exit_price}\t{t.stop_price}\t{t.target_price}\t{t.exit_reason}")

if __name__ == "__main__":
    main()
