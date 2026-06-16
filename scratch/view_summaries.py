import pandas as pd
from pathlib import Path

def main():
    opt_path = Path("data/backtests/ob_mitigation_optimization.csv")
    sum_path = Path("data/backtests/ob_mitigation_summary.csv")
    
    if opt_path.exists():
        df_opt = pd.read_csv(opt_path)
        print("Optimization grid columns:", df_opt.columns.tolist())
        print(df_opt.head(5).to_string(index=False))
        
    if sum_path.exists():
        df_sum = pd.read_csv(sum_path)
        print("\nSummary columns:", df_sum.columns.tolist())
        print(df_sum.head(5).to_string(index=False))

if __name__ == "__main__":
    main()
