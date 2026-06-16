"""Credit Spread R:R Optimizer.

Optimizes options credit spread returns by grid-searching underlying targets,
premium collection ratios (OTM vs ATM vs ITM), stop-loss anchors, and target capping.
"""

from __future__ import annotations

import csv
import itertools
import logging
import math
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from liquidity_universe import select_universe
from backtest_liquidity_sweep import (
    load_candles,
    prior_day_levels,
    score_setup,
    simulate_exit,
    BacktestParams,
    Trade
)

# Setup basic logging to file and stream
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("data/backtests/credit_spread_optimizer.log", encoding="utf-8"),
    ],
)


def calculate_credit_spread_rr(
    direction: str,
    entry_price: float,
    original_stop: float,
    exit_price: float,
    exit_reason: str,
    credit_premium_pct: float
) -> float:
    """Calculate Credit Spread realized R-multiple scaled by premium collection percentage."""
    # Max Loss is always normalized to -1.0R (representing the net risk budget, e.g. 1000 Rs)
    # Max profit (premium kept) relative to risk is: credit_premium_pct / (1.0 - credit_premium_pct)
    max_profit = credit_premium_pct / (1.0 - credit_premium_pct)
    
    if exit_reason == "TARGET":
        return max_profit

    if direction == "BULLISH":
        if exit_reason == "STOP":
            if exit_price <= original_stop:
                return -1.0
            # Trailed stop
            if exit_price >= entry_price:
                return max_profit
            # Linear decay interpolation between -1.0R and +max_profit
            return -1.0 + (1.0 + max_profit) * ((exit_price - original_stop) / (entry_price - original_stop))
        else:  # TIME_EXIT, NO_EXIT_BARS, etc.
            if exit_price >= entry_price:
                return max_profit
            if exit_price <= original_stop:
                return -1.0
            return -1.0 + (1.0 + max_profit) * ((exit_price - original_stop) / (entry_price - original_stop))
    else:  # BEARISH
        if exit_reason == "STOP":
            if exit_price >= original_stop:
                return -1.0
            # Trailed stop
            if exit_price <= entry_price:
                return max_profit
            # Linear decay interpolation between -1.0R and +max_profit
            return -1.0 + (1.0 + max_profit) * ((original_stop - exit_price) / (original_stop - entry_price))
        else:  # TIME_EXIT, NO_EXIT_BARS, etc.
            if exit_price <= entry_price:
                return max_profit
            if exit_price >= original_stop:
                return -1.0
            return -1.0 + (1.0 + max_profit) * ((original_stop - exit_price) / (original_stop - entry_price))


def find_vwap_crossing_candle(day_df: pd.DataFrame, idx: int, direction: str) -> pd.Series:
    """Find the specific candle that successfully crossed and closed past the VWAP line."""
    crossing_idx = idx
    for search_idx in range(idx, -1, -1):
        s_row = day_df.iloc[search_idx]
        s_close = float(s_row["close"])
        s_vwap = float(s_row["vwap"])
        if search_idx == 0:
            if (direction == "BULLISH" and s_close > s_vwap) or (direction == "BEARISH" and s_close < s_vwap):
                crossing_idx = search_idx
                break
        else:
            prev_row = day_df.iloc[search_idx - 1]
            prev_close = float(prev_row["close"])
            prev_vwap = float(prev_row["vwap"])
            if direction == "BULLISH" and s_close > s_vwap and prev_close <= prev_vwap:
                crossing_idx = search_idx
                break
            elif direction == "BEARISH" and s_close < s_vwap and prev_close >= prev_vwap:
                crossing_idx = search_idx
                break
    return day_df.iloc[crossing_idx]


def run_backtest_config(
    candles_dict: dict[str, pd.DataFrame],
    params: BacktestParams,
    stop_loss_type: str,
    rr_target: float,
    credit_premium_pct: float,
    cap_target: bool
) -> list[Trade]:
    """Run backtest with specific options parameters for Credit Spreads."""
    all_trades = []
    
    for symbol, candles in candles_dict.items():
        if candles.empty:
            continue
            
        dates = sorted(candles["date"].unique())
        if len(dates) < 2:
            continue
            
        previous_levels = {}
        for date in dates:
            day = candles[candles["date"] == date].reset_index(drop=True)
            if len(day) < 30:
                previous_levels[date] = prior_day_levels(day)
                continue
                
            prior_dates = [d for d in previous_levels if d < date]
            if not prior_dates:
                previous_levels[date] = prior_day_levels(day)
                continue
            prev = previous_levels[max(prior_dates)]
            
            opening = day[day["time"] < params.entry_start]
            if opening.empty:
                previous_levels[date] = prior_day_levels(day)
                continue
            orh = float(opening["high"].max())
            orl = float(opening["low"].min())
            
            scan = day[(day["time"] >= params.entry_start) & (day["time"] <= params.entry_cutoff)]
            bull_swept = False
            bear_swept = False
            sweep_low = None
            sweep_high = None
            traded_today = False
            
            for idx, row in scan.iterrows():
                if idx < params.swing_lookback + 1 or traded_today:
                    continue
                    
                close = float(row["close"])
                high = float(row["high"])
                low = float(row["low"])
                sweep_buffer = close * params.sweep_buffer_pct
                bull_level = min(float(prev["pdl"]), orl)
                bear_level = max(float(prev["pdh"]), orh)
                
                if low < bull_level - sweep_buffer:
                    bull_swept = True
                    sweep_low = low if sweep_low is None else min(sweep_low, low)
                if high > bear_level + sweep_buffer:
                    bear_swept = True
                    sweep_high = high if sweep_high is None else max(sweep_high, high)
                    
                previous_window = day.iloc[max(0, idx - params.swing_lookback):idx]
                swing_high = float(previous_window["high"].max())
                swing_low = float(previous_window["low"].min())
                volume_base = float(row.get("volume_median20") or 0.0)
                volume_ok = volume_base <= 0 or float(row["volume"]) >= volume_base * params.volume_multiplier
                atr = float(row.get("atr14") or 0.0)
                atr_pct = atr / close if close > 0 else 0.0
                atr_ok = 0.0005 <= atr_pct <= params.max_atr_pct
                
                # Enforce displacement filter gate >= 1.5 * atr_14
                atr_14 = float(row.get("atr14") or 0.0)
                if math.isnan(atr_14):
                    atr_14 = 0.0
                
                # BULLISH Signal Check
                bull_bos = close > swing_high and close > bull_level and close > float(row["vwap"])
                if bull_swept and bull_bos:
                    sweep_low_val = float(sweep_low or low)
                    impulse_leg_size = close - sweep_low_val
                    
                    if impulse_leg_size >= atr_14 * 1.5:
                        score, notes = score_setup(row, "BULLISH", True, True, volume_ok, atr_ok, params)
                        
                        # Mid-day lull raised score check
                        required_score = params.min_score
                        time_str = str(row["time"])
                        if "11:30" <= time_str <= "13:30":
                            required_score += 5
                            
                        if score >= required_score:
                            # Stop loss type assignment
                            if stop_loss_type == "sweep_extreme":
                                stop = min(float(sweep_low or low), bull_level) - sweep_buffer
                            elif stop_loss_type == "half_sweep":
                                extreme_stop = min(float(sweep_low or low), bull_level) - sweep_buffer
                                stop = close - (close - extreme_stop) * 0.5
                            elif stop_loss_type == "vwap_crossing":
                                crossing_candle = find_vwap_crossing_candle(day, idx, "BULLISH")
                                stop = float(crossing_candle["low"]) - 0.05
                                
                            risk = close - stop
                            if risk > 0:
                                # Target Assignment (Capped vs Uncapped)
                                standard_target = close + (risk * rr_target)
                                if cap_target and atr_14 > 0:
                                    daily_atr_estimate = atr_14 * 15
                                    day_open = float(day.iloc[0]["open"])
                                    expected_daily_high = day_open + daily_atr_estimate
                                    target = min(standard_target, expected_daily_high)
                                else:
                                    target = standard_target
                                    
                                original_stop = stop
                                exit_price, exit_dt, exit_reason, bars_elapsed, total_bars = simulate_exit(day, idx, "BULLISH", close, stop, target, params)
                                
                                realized_rr = calculate_credit_spread_rr(
                                    direction="BULLISH",
                                    entry_price=close,
                                    original_stop=original_stop,
                                    exit_price=exit_price,
                                    exit_reason=exit_reason,
                                    credit_premium_pct=credit_premium_pct
                                )
                                
                                pnl_points = realized_rr * risk
                                risk_amount = params.capital * params.risk_per_trade_pct
                                
                                all_trades.append(Trade(
                                    symbol=symbol,
                                    date=date,
                                    direction="BULLISH",
                                    entry_time=str(row["dt"]),
                                    exit_time=str(exit_dt),
                                    entry_price=round(close, 4),
                                    exit_price=round(exit_price, 4),
                                    stop_price=round(stop, 4),
                                    target_price=round(target, 4),
                                    risk_points=round(risk, 4),
                                    pnl_points=round(pnl_points, 4),
                                    rr_realized=round(realized_rr, 4),
                                    risk_amount=round(risk_amount, 2),
                                    pnl_amount=round(risk_amount * realized_rr, 2),
                                    score=score,
                                    exit_reason=exit_reason,
                                    setup_notes=f"Credit;{stop_loss_type};{rr_target}R;Prem={int(credit_premium_pct*100)}%;Cap={cap_target}",
                                ))
                                traded_today = True
                                continue

                # BEARISH Signal Check
                bear_bos = close < swing_low and close < bear_level and close < float(row["vwap"])
                if bear_swept and bear_bos:
                    sweep_high_val = float(sweep_high or high)
                    impulse_leg_size = sweep_high_val - close
                    
                    if impulse_leg_size >= atr_14 * 1.5:
                        score, notes = score_setup(row, "BEARISH", True, True, volume_ok, atr_ok, params)
                        
                        # Mid-day lull raised score check
                        required_score = params.min_score
                        time_str = str(row["time"])
                        if "11:30" <= time_str <= "13:30":
                            required_score += 5
                            
                        if score >= required_score:
                            # Stop loss type assignment
                            if stop_loss_type == "sweep_extreme":
                                stop = max(float(sweep_high or high), bear_level) + sweep_buffer
                            elif stop_loss_type == "half_sweep":
                                extreme_stop = max(float(sweep_high or high), bear_level) + sweep_buffer
                                stop = close + (extreme_stop - close) * 0.5
                            elif stop_loss_type == "vwap_crossing":
                                crossing_candle = find_vwap_crossing_candle(day, idx, "BEARISH")
                                stop = float(crossing_candle["high"]) + 0.05
                                
                            risk = stop - close
                            if risk > 0:
                                # Target Assignment
                                standard_target = close - (risk * rr_target)
                                if cap_target and atr_14 > 0:
                                    daily_atr_estimate = atr_14 * 15
                                    day_open = float(day.iloc[0]["open"])
                                    expected_daily_low = day_open - daily_atr_estimate
                                    target = max(standard_target, expected_daily_low)
                                else:
                                    target = standard_target
                                    
                                original_stop = stop
                                exit_price, exit_dt, exit_reason, bars_elapsed, total_bars = simulate_exit(day, idx, "BEARISH", close, stop, target, params)
                                
                                realized_rr = calculate_credit_spread_rr(
                                    direction="BEARISH",
                                    entry_price=close,
                                    original_stop=original_stop,
                                    exit_price=exit_price,
                                    exit_reason=exit_reason,
                                    credit_premium_pct=credit_premium_pct
                                )
                                
                                pnl_points = realized_rr * risk
                                risk_amount = params.capital * params.risk_per_trade_pct
                                
                                all_trades.append(Trade(
                                    symbol=symbol,
                                    date=date,
                                    direction="BEARISH",
                                    entry_time=str(row["dt"]),
                                    exit_time=str(exit_dt),
                                    entry_price=round(close, 4),
                                    exit_price=round(exit_price, 4),
                                    stop_price=round(stop, 4),
                                    target_price=round(target, 4),
                                    risk_points=round(risk, 4),
                                    pnl_points=round(pnl_points, 4),
                                    rr_realized=round(realized_rr, 4),
                                    risk_amount=round(risk_amount, 2),
                                    pnl_amount=round(risk_amount * realized_rr, 2),
                                    score=score,
                                    exit_reason=exit_reason,
                                    setup_notes=f"Credit;{stop_loss_type};{rr_target}R;Prem={int(credit_premium_pct*100)}%;Cap={cap_target}",
                                ))
                                traded_today = True
                                continue
            
            previous_levels[date] = prior_day_levels(day)
            
    return all_trades


def calculate_max_drawdown(trades: list[Trade]) -> float:
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for t in trades:
        equity += float(t.rr_realized)
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    return drawdown


def main():
    logging.info("Starting Credit Spread Specific R:R Optimizer...")
    symbols_to_run = ["NIFTY", "SENSEX", "RELIANCE"]
    candles_dict = {}
    
    for symbol in symbols_to_run:
        path = Path(f"data/historical/{symbol}_5m.csv")
        if path.exists():
            candles_dict[symbol] = load_candles(path)
            
    if not candles_dict:
        logging.critical("No historical data found! Exiting...")
        return
        
    params = BacktestParams(swing_lookback=5)
    
    # Define optimization grid parameter lists
    stop_loss_types = ["sweep_extreme", "half_sweep", "vwap_crossing"]
    rr_targets = [0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5]
    credit_premium_pcts = [0.30, 0.40, 0.50, 0.60, 0.70]
    cap_targets = [True, False]
    
    results = []
    
    # Run combinations
    grid = list(itertools.product(stop_loss_types, rr_targets, credit_premium_pcts, cap_targets))
    logging.info("Running optimization search over %d Credit Spread configurations...", len(grid))
    
    for idx, (stop_type, rr_target, prem_pct, cap) in enumerate(grid, 1):
        trades = run_backtest_config(candles_dict, params, stop_type, rr_target, prem_pct, cap)
        
        if not trades:
            continue
            
        rrs = [float(t.rr_realized) for t in trades]
        wins = [rr for rr in rrs if rr > 0]
        win_rate = len(wins) / len(trades)
        total_rr = sum(rrs)
        expectancy = total_rr / len(trades)
        max_dd = calculate_max_drawdown(trades)
        
        results.append({
            "stop_loss_type": stop_type,
            "rr_target": rr_target,
            "credit_premium_pct": prem_pct,
            "cap_target": cap,
            "trades": len(trades),
            "win_rate": round(win_rate, 4),
            "total_rr": round(total_rr, 4),
            "expectancy_rr": round(expectancy, 4),
            "max_drawdown_rr": round(max_dd, 4)
        })
        
    # Sort results by Expectancy
    results = sorted(results, key=lambda x: x["expectancy_rr"], reverse=True)
    
    # Write to CSV
    output_path = Path("data/backtests/credit_spread_optimization.csv")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    fieldnames = ["stop_loss_type", "rr_target", "credit_premium_pct", "cap_target", "trades", "win_rate", "total_rr", "expectancy_rr", "max_drawdown_rr"]
    try:
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(results)
        logging.info("Successfully wrote Credit Spread report to %s", output_path)
    except PermissionError:
        backup_path = Path("data/backtests_temp/credit_spread_optimization.csv")
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        with open(backup_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(results)
        logging.info("Wrote Credit Spread report backup to %s.", backup_path)

    # Print Top 15 configurations
    logging.info("--- TOP 15 OPTIMIZED CREDIT SPREAD CONFIGURATIONS ---")
    for rank, res in enumerate(results[:15], 1):
        max_profit_r = res["credit_premium_pct"] / (1.0 - res["credit_premium_pct"])
        logging.info(
            "Rank %d: Stop=%s | Target=%.2fR (Cap=%s) | Prem=%d%% (MaxProfit=%.2fR) | WinRate=%.2f%% | TotalRR=%.2fR | Expectancy=%.2fR",
            rank,
            res["stop_loss_type"],
            res["rr_target"],
            res["cap_target"],
            int(res["credit_premium_pct"] * 100),
            max_profit_r,
            res["win_rate"] * 100,
            res["total_rr"],
            res["expectancy_rr"]
        )


if __name__ == "__main__":
    main()
