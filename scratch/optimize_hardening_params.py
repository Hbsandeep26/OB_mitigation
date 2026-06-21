import sys
import os
import itertools
import pandas as pd
from pathlib import Path

# Add project root to sys.path
sys.path.append(str(Path(__file__).parent.parent))

import config
from backtest_orderblock_mitigation import load_candles, backtest_ob_mitigation, BacktestParams, summarize

def main():
    symbols = ["NIFTY", "SENSEX", "RELIANCE"]
    input_dir = Path(__file__).parent.parent / "data" / "historical"
    
    print("Loading data...")
    data_dict = {}
    for sym in symbols:
        path_5m = input_dir / f"{sym}_5m.csv"
        path_1m = input_dir / f"{sym}_1m.csv"
        df_5m = load_candles(path_5m)
        df_1m = load_candles(path_1m)
        df_5m_filtered = df_5m[df_5m["date"] >= df_1m["date"].min()].copy()
        df_1m_filtered = df_1m[df_1m["date"] >= df_5m_filtered["date"].min()].copy()
        data_dict[sym] = (df_5m_filtered, df_1m_filtered)
    print("Data loaded.")

    # Grid values
    scores = [70, 80, 90]
    min_rrs = [1.0, 1.25, 1.5, 1.75]
    drifts = [0.25, 0.5, 1.0]
    slippages = [0.0, 1.5, 3.0] # in BPS
    
    params = BacktestParams(
        strategy_type="Ratio",
        trigger_type="choch",
        stop_loss_type="5m_origin",
        pivot_len=5,
        entry_retracement=0.618,
        target_type="extreme",
        discrete_risk_budget=1000.0,
        capital=200000.0,
        max_trades_per_day=3,
        use_vwap_filter=True
    )
    
    results = []
    
    combinations = list(itertools.product(scores, min_rrs, drifts, slippages))
    total = len(combinations)
    print(f"Starting grid search over {total} combinations...")
    
    for idx, (score, min_rr, drift, slip) in enumerate(combinations, 1):
        # Override config attributes
        config.MTF_MIN_SCORE = score
        config.MTF_MIN_LIVE_RR = min_rr
        config.MTF_MAX_ENTRY_DRIFT_ATR = drift
        config.MTF_SLIPPAGE_BUFFER_BPS = slip
        
        all_trades = []
        for sym in symbols:
            df_5m_f, df_1m_f = data_dict[sym]
            trades = backtest_ob_mitigation(sym, df_5m_f, df_1m_f, params)
            all_trades.extend(trades)
            
        summary = summarize("PORTFOLIO", all_trades)
        
        results.append({
            "score": score,
            "min_rr": min_rr,
            "drift": drift,
            "slippage": slip,
            "trades": summary["trades"],
            "win_rate": summary["win_rate"],
            "expectancy": summary["expectancy_rr"],
            "total_rr": summary["total_rr"],
            "max_dd": summary["max_drawdown_rr"]
        })
        
        if idx % 10 == 0 or idx == total:
            print(f"[{idx}/{total}] Score={score}, RR={min_rr}, Drift={drift}, Slip={slip} -> Trades={summary['trades']}, WR={summary['win_rate']*100:.1f}%, Expectancy={summary['expectancy_rr']:.2f}R, Total={summary['total_rr']:.2f}R")
            
    # Sort and display top results
    df_res = pd.DataFrame(results)
    df_res = df_res.sort_values(by="total_rr", ascending=False)
    print("\nTOP 20 CONFIGURATIONS BY TOTAL RR:")
    print(df_res.head(20).to_string(index=False))
    
    df_res.to_csv("C:/Users/sande/Antigravity_upstox_selling/Dhan_Algo/scratch/custom_optimization_results.csv", index=False)
    print("Saved custom optimization results to scratch/custom_optimization_results.csv")

if __name__ == "__main__":
    main()
