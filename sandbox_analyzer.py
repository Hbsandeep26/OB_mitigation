import os
import pandas as pd
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_LOG_FILE = os.path.join(BASE_DIR, "sandbox_trade_logs.csv")
REPORTS_DIR = os.path.join(BASE_DIR, "reports")

def load_trades():
    if not os.path.exists(CSV_LOG_FILE):
        return pd.DataFrame()
    try:
        df = pd.read_csv(CSV_LOG_FILE)
        return df
    except Exception as e:
        print(f"Error reading trade logs: {e}")
        return pd.DataFrame()

def generate_daily_summary(df):
    if df.empty or "Action" not in df.columns or "Timestamp" not in df.columns:
        return pd.DataFrame()
    
    # Filter for exits
    exits = df[df["Action"].str.upper() == "EXIT"].copy()
    if exits.empty:
        return pd.DataFrame()
        
    exits["Datetime"] = pd.to_datetime(exits["Timestamp"], errors='coerce')
    exits = exits.dropna(subset=["Datetime"])
    if exits.empty:
        return pd.DataFrame()
        
    exits["Date"] = exits["Datetime"].dt.date
    
    # Group by Date
    grouped = exits.groupby("Date")
    
    daily_stats = []
    for date, group in grouped:
        total_trades = len(group)
        pnl_col = group["PnL"].fillna(0.0).astype(float)
        total_pnl = pnl_col.sum()
        wins = sum(pnl_col > 0)
        losses = sum(pnl_col <= 0)
        win_rate = (wins / total_trades) * 100.0 if total_trades > 0 else 0.0
        
        daily_stats.append({
            "Date": date.strftime("%Y-%m-%d"),
            "Trades": total_trades,
            "Wins": wins,
            "Losses": losses,
            "Win Rate (%)": round(win_rate, 2),
            "Net PnL (INR)": round(total_pnl, 2)
        })
        
    daily_df = pd.DataFrame(daily_stats)
    # Sort descending by date
    if not daily_df.empty:
        daily_df = daily_df.sort_values(by="Date", ascending=False)
    return daily_df

def generate_weekly_summary(df):
    if df.empty or "Action" not in df.columns or "Timestamp" not in df.columns:
        return pd.DataFrame()
        
    # Filter for exits
    exits = df[df["Action"].str.upper() == "EXIT"].copy()
    if exits.empty:
        return pd.DataFrame()
        
    exits["Datetime"] = pd.to_datetime(exits["Timestamp"], errors='coerce')
    exits = exits.dropna(subset=["Datetime"])
    if exits.empty:
        return pd.DataFrame()
        
    # Find Monday of the week
    exits["Week_Start"] = exits["Datetime"].apply(lambda dt: (dt - timedelta(days=dt.weekday())).date())
    
    grouped = exits.groupby("Week_Start")
    
    weekly_stats = []
    for week_start, group in grouped:
        total_trades = len(group)
        pnl_col = group["PnL"].fillna(0.0).astype(float)
        total_pnl = pnl_col.sum()
        wins = sum(pnl_col > 0)
        losses = sum(pnl_col <= 0)
        win_rate = (wins / total_trades) * 100.0 if total_trades > 0 else 0.0
        
        # Calculate weekly max drawdown
        equity = []
        running_total = 0.0
        for val in pnl_col:
            running_total += val
            equity.append(running_total)
        
        # Max drawdown from equity curve
        peak = -float('inf')
        max_dd = 0.0
        for value in equity:
            peak = max(peak, value)
            max_dd = min(max_dd, value - peak)
        max_dd = abs(max_dd)
        
        week_end = week_start + timedelta(days=6)
        
        weekly_stats.append({
            "Week Range": f"{week_start.strftime('%Y-%m-%d')} to {week_end.strftime('%Y-%m-%d')}",
            "Trades": total_trades,
            "Wins": wins,
            "Losses": losses,
            "Win Rate (%)": round(win_rate, 2),
            "Max Drawdown (INR)": round(max_dd, 2),
            "Net PnL (INR)": round(total_pnl, 2)
        })
        
    weekly_df = pd.DataFrame(weekly_stats)
    # Sort descending by week start date
    if not weekly_df.empty:
        weekly_df = weekly_df.sort_values(by="Week Range", ascending=False)
    return weekly_df

def save_reports(daily_df, weekly_df):
    os.makedirs(REPORTS_DIR, exist_ok=True)
    
    daily_path = os.path.join(REPORTS_DIR, "daily_performance.csv")
    weekly_path = os.path.join(REPORTS_DIR, "weekly_performance.csv")
    
    if not daily_df.empty:
        daily_df.to_csv(daily_path, index=False)
        print(f"Saved daily summary to {daily_path}")
        
    if not weekly_df.empty:
        weekly_df.to_csv(weekly_path, index=False)
        print(f"Saved weekly summary to {weekly_path}")

def run_analysis():
    trades_df = load_trades()
    if trades_df.empty:
        print("No sandbox trade logs found to analyze.")
        return pd.DataFrame(), pd.DataFrame()
        
    daily_summary = generate_daily_summary(trades_df)
    weekly_summary = generate_weekly_summary(trades_df)
    
    save_reports(daily_summary, weekly_summary)
    
    return daily_summary, weekly_summary

if __name__ == "__main__":
    print("Running Sandbox Performance Analysis...")
    daily, weekly = run_analysis()
    if not daily.empty:
        print("\n--- Daily Performance Summary (Recent First) ---")
        print(daily.to_string(index=False))
    if not weekly.empty:
        print("\n--- Weekly Performance Summary (Recent First) ---")
        print(weekly.to_string(index=False))
