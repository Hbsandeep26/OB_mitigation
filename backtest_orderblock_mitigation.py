"""Backtest Multi-Timeframe Order Block & Liquidity Tap Options Strategy.

Identifies 5m structural Order Blocks and daily S/R levels, triggers entries 
on 1m candles using Swing Failure Patterns (SFP) or Change of Character (ChoCH), 
and implements tight stop-losses placed below swing lows.
"""

from __future__ import annotations

import argparse
import csv
import itertools
import logging
import math
from dataclasses import dataclass, asdict
from pathlib import Path

import pandas as pd

from liquidity_universe import batch_universe, select_universe


@dataclass
class BacktestParams:
    pivot_len: int = 5
    displacement_multiplier: float = 1.5
    invalidation_buffer: float = 0.25
    max_leg_age: int = 150
    entry_retracement: float = 0.618
    target_type: str = "extreme"  # "extreme", "rr_ratio"
    target_rr: float = 3.0
    capital: float = 200000.0
    risk_per_trade_pct: float = 0.005
    entry_start: str = "09:30"
    entry_cutoff: str = "13:45"
    exit_time: str = "15:00"
    strategy_type: str = "Ratio"  # "Naked ATM Buy", "Debit Spread", "Credit Spread", "Ratio"
    credit_premium_pct: float = 0.50
    discrete_risk_budget: float | None = 1000.0
    nifty_spread_width: float = 10.0
    sensex_spread_width: float = 20.0
    reliance_spread_width: float = 2.0
    trigger_type: str = "choch"  # "none", "ma_crossover", "sfp", "choch"
    stop_loss_type: str = "5m_origin"  # "5m_origin", "1m_candle_low", "1m_atr_1.5"
    max_trades_per_day: int = 3
    use_vwap_filter: bool = True


@dataclass
class Trade:
    symbol: str
    date: str
    direction: str
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    stop_price: float
    target_price: float
    risk_points: float
    pnl_points: float
    rr_realized: float
    risk_amount: float
    pnl_amount: float
    score: int
    exit_reason: str
    setup_notes: str


TRADE_FIELDS = [
    "symbol",
    "date",
    "direction",
    "entry_time",
    "exit_time",
    "entry_price",
    "exit_price",
    "stop_price",
    "target_price",
    "risk_points",
    "pnl_points",
    "rr_realized",
    "risk_amount",
    "pnl_amount",
    "score",
    "exit_reason",
    "setup_notes",
]


def setup_logging(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(output_dir / "backtest_orderblock_mitigation.log", encoding="utf-8"),
        ],
    )


def load_candles(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    if df.empty:
        return df
    if "datetime" not in df.columns:
        raise ValueError(f"{path} has no datetime column")
    df["dt"] = pd.to_datetime(df["datetime"], errors="coerce")
    df = df.dropna(subset=["dt"]).sort_values("dt").copy()
    df["date"] = df["dt"].dt.date.astype(str)
    df["time"] = df["dt"].dt.strftime("%H:%M")
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"])
    df["volume"] = df["volume"].fillna(0.0)
    
    # Calculate daily VWAP
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    df["pv"] = typical * df["volume"].clip(lower=1.0)
    df["day_cum_pv"] = df.groupby("date")["pv"].cumsum()
    df["day_cum_volume"] = df.groupby("date")["volume"].transform(lambda s: s.clip(lower=1.0).cumsum())
    df["vwap"] = df["day_cum_pv"] / df["day_cum_volume"]
    
    # Calculate ATR14
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr14"] = tr.rolling(14, min_periods=5).mean()
    df["volume_median20"] = df["volume"].rolling(20, min_periods=5).median()
    
    # Add daily High/Low/Close levels
    daily = df.groupby("date").agg(
        pd_high=("high", "max"),
        pd_low=("low", "min"),
        pd_close=("close", "last")
    ).shift(1)  # Shift to represent previous day levels
    
    df = df.merge(daily, left_on="date", right_index=True, how="left")
    return df


def get_strike_step(symbol: str) -> float:
    steps = {
        "NIFTY": 50.0,
        "SENSEX": 100.0,
        "BANKNIFTY": 100.0,
        "RELIANCE": 20.0,
        "HDFCBANK": 10.0,
        "AXISBANK": 10.0,
        "BAJFINANCE": 50.0,
        "ADANIENT": 20.0,
        "ADANIPORTS": 10.0,
        "ASIANPAINT": 20.0,
        "APOLLOHOSP": 50.0,
    }
    return steps.get(symbol.upper(), 10.0)


def get_margin_per_lot(symbol: str, close_price: float) -> float:
    symbol = symbol.upper()
    if symbol == "NIFTY":
        return 120000.0
    elif symbol == "SENSEX":
        return 100000.0
    elif symbol == "BANKNIFTY":
        return 120000.0
    else:
        nse_lot_sizes = {
            "RELIANCE": 250,
            "HDFCBANK": 550,
            "AXISBANK": 625,
            "BAJFINANCE": 125,
            "ADANIENT": 300,
            "ADANIPORTS": 650,
            "ASIANPAINT": 200,
            "APOLLOHOSP": 125
        }
        std_lot = nse_lot_sizes.get(symbol, 250)
        margin_per_std_lot = 250000.0
        backtester_lot_size = 1
        return margin_per_std_lot * (backtester_lot_size / std_lot)


def calculate_realized_rr(
    strategy_type: str,
    direction: str,
    entry_price: float,
    original_stop: float,
    exit_price: float,
    exit_reason: str,
    rr_target: float,
    bars_elapsed: int,
    total_bars: int,
    credit_premium_pct: float = 0.50,
    strike_step: float = 50.0
) -> float:
    """Calculate option P&L realized R-multiple based on strategy parameters."""
    risk = abs(entry_price - original_stop)
    if risk <= 0:
        return -1.0

    if strategy_type == "Naked ATM Buy":
        delta = 0.50
        if direction == "BULLISH":
            r_linear = delta * (exit_price - entry_price) / risk
        else:
            r_linear = delta * (entry_price - exit_price) / risk
            
        time_frac = min(1.0, max(0.0, bars_elapsed / max(1, total_bars)))
        theta_cost = 0.40 * time_frac  # Maximum 0.4R lost if held all day
        
        rr_realized = r_linear - theta_cost
        return max(-1.0, rr_realized)

    elif strategy_type == "Debit Spread":
        max_profit = (1.0 - credit_premium_pct) / credit_premium_pct
        if exit_reason == "TARGET":
            return max_profit
        if direction == "BULLISH":
            r_linear = max_profit * (exit_price - entry_price) / risk
        else:
            r_linear = max_profit * (entry_price - exit_price) / risk
            
        return min(max_profit, max(-1.0, r_linear))

    elif strategy_type == "Credit Spread":
        max_profit = credit_premium_pct / (1.0 - credit_premium_pct)
        if exit_reason == "TARGET":
            return max_profit
            
        if direction == "BULLISH":
            if exit_reason == "STOP":
                if exit_price <= original_stop:
                    return -1.0
                if exit_price >= entry_price:
                    return max_profit
                return -1.0 + (1.0 + max_profit) * ((exit_price - original_stop) / (entry_price - original_stop))
            else:  # TIME_EXIT
                if exit_price >= entry_price:
                    return max_profit
                if exit_price <= original_stop:
                    return -1.0
                return -1.0 + (1.0 + max_profit) * ((exit_price - original_stop) / (entry_price - original_stop))
        else:  # BEARISH
            if exit_reason == "STOP":
                if exit_price >= original_stop:
                    return -1.0
                if exit_price <= entry_price:
                    return max_profit
                return -1.0 + (1.0 + max_profit) * ((original_stop - exit_price) / (original_stop - entry_price))
            else:  # TIME_EXIT
                if exit_price <= entry_price:
                    return max_profit
                if exit_price >= original_stop:
                    return -1.0
                return -1.0 + (1.0 + max_profit) * ((original_stop - exit_price) / (original_stop - entry_price))

    elif strategy_type == "Ratio":
        if exit_reason == "TARGET":
            return rr_target
        if exit_reason == "STOP":
            return -1.0
        if exit_reason in ("TIME_EXIT", "NO_EXIT_BARS"):
            k_long = entry_price
            if direction == "BULLISH":
                k_short = k_long + strike_step
                if exit_price <= k_long:
                    return 0.25
                elif exit_price <= k_short:
                    return 0.25 + (rr_target - 0.25) * ((exit_price - k_long) / strike_step)
                else:
                    return max(-1.0, rr_target - (rr_target - 0.25) * ((exit_price - k_short) / strike_step))
            else:  # BEARISH
                k_short = k_long - strike_step
                if exit_price >= k_long:
                    return 0.25
                elif exit_price >= k_short:
                    return 0.25 + (rr_target - 0.25) * ((k_long - exit_price) / strike_step)
                else:
                    return max(-1.0, rr_target - (rr_target - 0.25) * ((k_short - exit_price) / strike_step))

    elif strategy_type == "Synthetic Future":
        if exit_reason == "TARGET":
            return rr_target
        if exit_reason == "STOP":
            return -1.0
        if exit_reason in ("TIME_EXIT", "NO_EXIT_BARS"):
            if direction == "BULLISH":
                return (exit_price - entry_price) / risk
            else:
                return (entry_price - exit_price) / risk

    return 0.0


def calculate_pivots(df: pd.DataFrame, L: int) -> tuple[list[float | None], list[int | None], list[float | None], list[int | None]]:
    """Calculate pivots with a confirmation delay of L bars."""
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)
    
    pivot_hi_vals = [None] * n
    pivot_hi_idxs = [None] * n
    pivot_lo_vals = [None] * n
    pivot_lo_idxs = [None] * n
    
    last_hi_val, last_hi_idx = None, None
    last_lo_val, last_lo_idx = None, None
    
    for i in range(2 * L, n):
        target_idx = i - L
        
        is_hi = True
        val_hi = highs[target_idx]
        for j in range(target_idx - L, target_idx + L + 1):
            if highs[j] > val_hi:
                is_hi = False
                break
        if is_hi:
            last_hi_val = val_hi
            last_hi_idx = target_idx
            
        is_lo = True
        val_lo = lows[target_idx]
        for j in range(target_idx - L, target_idx + L + 1):
            if lows[j] < val_lo:
                is_lo = False
                break
        if is_lo:
            last_lo_val = val_lo
            last_lo_idx = target_idx
            
        pivot_hi_vals[i] = last_hi_val
        pivot_hi_idxs[i] = last_hi_idx
        pivot_lo_vals[i] = last_lo_val
        pivot_lo_idxs[i] = last_lo_idx
        
    return pivot_hi_vals, pivot_hi_idxs, pivot_lo_vals, pivot_lo_idxs


def run_5m_ob_tracker(df_5m: pd.DataFrame, params: BacktestParams) -> pd.DataFrame:
    """Track 5-minute structural swing high/low, BOS, and Order Blocks (OB)."""
    L = params.pivot_len
    pivot_hi_vals, pivot_hi_idxs, pivot_lo_vals, pivot_lo_idxs = calculate_pivots(df_5m, L)
    
    trend_col = [0] * len(df_5m)
    ob_low_col = [0.0] * len(df_5m)
    ob_high_col = [0.0] * len(df_5m)
    zone_entry_col = [0.0] * len(df_5m)
    stop_loss_col = [0.0] * len(df_5m)
    take_profit_col = [0.0] * len(df_5m)
    ob_time_col = [""] * len(df_5m)
    
    trend = 0
    ob_low, ob_high = 0.0, 0.0
    zone_entry_price = 0.0
    stop_loss = 0.0
    take_profit = 0.0
    leg_age = 0
    ob_time = ""
    
    highs = df_5m["high"].values
    lows = df_5m["low"].values
    closes = df_5m["close"].values
    opens = df_5m["open"].values
    atr14 = df_5m["atr14"].values
    
    for i in range(2 * L, len(df_5m)):
        # Check invalidation
        if trend != 0:
            close_price = closes[i]
            if trend == 1:
                if close_price < ob_low or leg_age > params.max_leg_age:
                    trend = 0
                    ob_time = ""
                else:
                    leg_age += 1
            elif trend == -1:
                if close_price > ob_high or leg_age > params.max_leg_age:
                    trend = 0
                    ob_time = ""
                else:
                    leg_age += 1
                    
        # Check new BOS
        close_price = closes[i]
        last_hi_val = pivot_hi_vals[i]
        last_hi_idx = pivot_hi_idxs[i]
        last_lo_val = pivot_lo_vals[i]
        last_lo_idx = pivot_lo_idxs[i]
        
        if last_hi_val is not None and close_price > last_hi_val:
            start_search = last_hi_idx
            origin_low_val = min(lows[start_search:i+1])
            origin_low_idx_offset = lows[start_search:i+1].argmin()
            origin_low_idx = start_search + origin_low_idx_offset
            
            impulse_high_val = max(highs[origin_low_idx:i+1])
            leg_size = impulse_high_val - origin_low_val
            curr_atr = atr14[i] if not math.isnan(atr14[i]) else 1.0
            
            if leg_size >= params.displacement_multiplier * curr_atr:
                trend = 1
                leg_age = 0
                
                # Find last bearish candle before breakout began
                ob_idx = origin_low_idx
                for k in range(i, origin_low_idx - 1, -1):
                    if closes[k] < opens[k]:
                        ob_idx = k
                        break
                
                ob_low = lows[ob_idx]
                ob_high = highs[ob_idx]
                ob_time = str(df_5m["time"].values[ob_idx])
                
                # Confluence zone overlaps Golden Zone and OB
                golden_low = impulse_high_val - 0.786 * leg_size
                golden_high = impulse_high_val - 0.618 * leg_size
                
                zone_entry_price = min(ob_high, golden_high)
                if zone_entry_price <= ob_low:
                    zone_entry_price = golden_high
                    
                stop_loss = ob_low - params.invalidation_buffer * curr_atr
                take_profit = impulse_high_val
 
        elif last_lo_val is not None and close_price < last_lo_val:
            start_search = last_lo_idx
            origin_high_val = max(highs[start_search:i+1])
            origin_high_idx_offset = highs[start_search:i+1].argmax()
            origin_high_idx = start_search + origin_high_idx_offset
            
            impulse_low_val = min(lows[origin_high_idx:i+1])
            leg_size = origin_high_val - impulse_low_val
            curr_atr = atr14[i] if not math.isnan(atr14[i]) else 1.0
            
            if leg_size >= params.displacement_multiplier * curr_atr:
                trend = -1
                leg_age = 0
                
                # Find last bullish candle before breakout began
                ob_idx = origin_high_idx
                for k in range(i, origin_high_idx - 1, -1):
                    if closes[k] > opens[k]:
                        ob_idx = k
                        break
                
                ob_low = lows[ob_idx]
                ob_high = highs[ob_idx]
                ob_time = str(df_5m["time"].values[ob_idx])
                
                golden_low = impulse_low_val + 0.618 * leg_size
                golden_high = impulse_low_val + 0.786 * leg_size
                
                zone_entry_price = max(ob_low, golden_low)
                if zone_entry_price >= ob_high:
                    zone_entry_price = golden_low
                    
                stop_loss = ob_high + params.invalidation_buffer * curr_atr
                take_profit = impulse_low_val
                
        trend_col[i] = trend
        ob_low_col[i] = ob_low
        ob_high_col[i] = ob_high
        zone_entry_col[i] = zone_entry_price
        stop_loss_col[i] = stop_loss
        take_profit_col[i] = take_profit
        ob_time_col[i] = ob_time
        
    df_state = pd.DataFrame({
        "dt": df_5m["dt"],
        "trend": trend_col,
        "ob_low": ob_low_col,
        "ob_high": ob_high_col,
        "zone_entry_price": zone_entry_col,
        "stop_loss_5m": stop_loss_col,
        "take_profit_5m": take_profit_col,
        "ob_time": ob_time_col
    })
    return df_state


def compute_1m_triggers(df_1m: pd.DataFrame, trigger_type: str) -> tuple[pd.Series, pd.Series]:
    """Compute entry signals for 1m candles based on technical indicators."""
    closes = df_1m["close"]
    highs = df_1m["high"]
    lows = df_1m["low"]
    opens = df_1m["open"]
    n = len(df_1m)
    
    bull_trigger = pd.Series([False] * n, index=df_1m.index)
    bear_trigger = pd.Series([False] * n, index=df_1m.index)
    
    if trigger_type == "none":
        bull_trigger = pd.Series([True] * n, index=df_1m.index)
        bear_trigger = pd.Series([True] * n, index=df_1m.index)
        
    elif trigger_type == "ma_crossover":
        ema5 = closes.ewm(span=5, adjust=False).mean()
        ema13 = closes.ewm(span=13, adjust=False).mean()
        bull_trigger = (ema5 > ema13) & (ema5.shift(1) <= ema13.shift(1))
        bear_trigger = (ema5 < ema13) & (ema5.shift(1) >= ema13.shift(1))
        
    elif trigger_type == "sfp":
        # Swing Failure Pattern (SFP)
        min5 = lows.rolling(5).min().shift(1)
        bull_trigger = (lows < min5) & (closes > min5)
        
        max5 = highs.rolling(5).max().shift(1)
        bear_trigger = (highs > max5) & (closes < max5)
        
    elif trigger_type == "choch":
        # 1m Change of Character (ChoCH)
        max3 = highs.rolling(3).max().shift(1)
        bull_trigger = (closes > max3) & (closes.shift(1) <= max3.shift(1))
        
        min3 = lows.rolling(3).min().shift(1)
        bear_trigger = (closes < min3) & (closes.shift(1) >= min3.shift(1))
        
    return bull_trigger, bear_trigger


def backtest_ob_mitigation(symbol: str, df_5m: pd.DataFrame, df_1m: pd.DataFrame, params: BacktestParams) -> list[Trade]:
    trades = []
    
    # 1. Run 5m State tracker
    df_state = run_5m_ob_tracker(df_5m, params)
    
    # 2. Align 5m states to 1m candles without lookahead bias
    df_state["dt"] = pd.to_datetime(df_state["dt"])
    df_1m["dt"] = pd.to_datetime(df_1m["dt"])
    df_state = df_state.sort_values("dt")
    df_1m = df_1m.sort_values("dt")
    
    # Pre-compute triggers
    bull_trig, bear_trig = compute_1m_triggers(df_1m, params.trigger_type)
    df_1m["bull_trigger"] = bull_trig
    df_1m["bear_trigger"] = bear_trig
    
    # Merge
    merged = pd.merge_asof(df_1m, df_state, on="dt", direction="backward")
    
    # Simulation state variables
    trade_active = False
    trade_entry_idx = None
    trade_entry_price = None
    trade_direction = None
    trade_stop = None
    trade_target = None
    trade_score = 0
    trade_notes = []
    
    pending_pullback = False
    pending_direction = None
    pending_zone_entry = None
    pending_stop_5m = None
    pending_target_5m = None
    pending_ob_low = None
    pending_ob_high = None
    trades_today = 0
    
    highs = merged["high"].values
    lows = merged["low"].values
    closes = merged["close"].values
    opens = merged["open"].values
    times = merged["time"].values
    dts = merged["dt"].values
    atr14 = merged["atr14"].values
    vwaps = merged["vwap"].values
    volumes = merged["volume"].values
    volume_median20 = merged["volume_median20"].values
    
    # Merged 5m state columns
    trends_5m = merged["trend"].values
    ob_lows_5m = merged["ob_low"].values
    ob_highs_5m = merged["ob_high"].values
    zone_entries_5m = merged["zone_entry_price"].values
    stops_5m = merged["stop_loss_5m"].values
    targets_5m = merged["take_profit_5m"].values
    
    # Previous day levels
    pd_highs = merged["pd_high"].values
    pd_lows = merged["pd_low"].values
    pd_closes = merged["pd_close"].values
    
    bull_triggers = merged["bull_trigger"].values
    bear_triggers = merged["bear_trigger"].values
    
    strike_step = get_strike_step(symbol)
    n = len(merged)
    
    for i in range(20, n):
        # Reset trades_today and pending state on date transition
        curr_date = str(merged.iloc[i]["date"])
        if i > 20 and str(merged.iloc[i-1]["date"]) != curr_date:
            trades_today = 0
            pending_pullback = False
            pending_direction = None
            pending_zone_entry = None
            pending_stop_5m = None
            pending_target_5m = None
            pending_ob_low = None
            pending_ob_high = None
            
        # A. Trade exit tracking
        if trade_active:
            open_price = opens[i]
            low_price = lows[i]
            high_price = highs[i]
            close_price = closes[i]
            time_str = times[i]
            dt_val = dts[i]
            
            exit_price = None
            exit_reason = None
            exit_dt = None
            
            if trade_direction == "BULLISH":
                if low_price <= trade_stop:
                    exit_price = open_price if open_price <= trade_stop else trade_stop
                    exit_reason = "STOP"
                    exit_dt = dt_val
                elif high_price >= trade_target:
                    exit_price = open_price if open_price >= trade_target else trade_target
                    exit_reason = "TARGET"
                    exit_dt = dt_val
                elif time_str >= params.exit_time:
                    exit_price = close_price
                    exit_reason = "TIME_EXIT"
                    exit_dt = dt_val
            else: # BEARISH
                if high_price >= trade_stop:
                    exit_price = open_price if open_price >= trade_stop else trade_stop
                    exit_reason = "STOP"
                    exit_dt = dt_val
                elif low_price <= trade_target:
                    exit_price = open_price if open_price >= trade_target else trade_target
                    exit_reason = "TARGET"
                    exit_dt = dt_val
                elif time_str >= params.exit_time:
                    exit_price = close_price
                    exit_reason = "TIME_EXIT"
                    exit_dt = dt_val
                    
            if exit_reason is not None:
                bars_elapsed = i - trade_entry_idx
                total_bars = 375
                
                realized_rr_target = params.target_rr
                if params.target_type == "extreme" and params.strategy_type != "Ratio":
                    underlying_risk = abs(trade_entry_price - trade_stop)
                    underlying_reward = abs(trade_target - trade_entry_price)
                    realized_rr_target = underlying_reward / underlying_risk if underlying_risk > 0 else 1.0
                
                rr_realized = calculate_realized_rr(
                    strategy_type=params.strategy_type,
                    direction=trade_direction,
                    entry_price=trade_entry_price,
                    original_stop=trade_stop,
                    exit_price=exit_price,
                    exit_reason=exit_reason,
                    rr_target=realized_rr_target,
                    bars_elapsed=bars_elapsed,
                    total_bars=total_bars,
                    credit_premium_pct=params.credit_premium_pct,
                    strike_step=strike_step
                )
                
                risk_pts = abs(trade_entry_price - trade_stop)
                lot_size = 75 if symbol == "NIFTY" else (10 if symbol == "SENSEX" else (250 if symbol == "RELIANCE" else 1))
                
                if params.discrete_risk_budget is not None and params.discrete_risk_budget > 0:
                    if params.strategy_type == "Ratio":
                        margin_per_lot = get_margin_per_lot(symbol, trade_entry_price)
                        risk_budget_rupees = params.discrete_risk_budget
                        risk_per_lot = 0.20 * strike_step * lot_size
                        lots_by_risk = math.floor(risk_budget_rupees / risk_per_lot)
                        lots_by_margin = math.floor(params.capital / margin_per_lot)
                        lots = min(lots_by_risk, lots_by_margin)
                        if lots < 1:
                            lots = 1
                        risk_amount = lots * risk_per_lot
                        pnl_amount = risk_amount * rr_realized
                        pnl_points = rr_realized * (0.20 * strike_step)
                        trade_notes.append(f"lots={lots};margin={lots * margin_per_lot}")
                    elif params.strategy_type == "Synthetic Future":
                        margin_per_lot = get_margin_per_lot(symbol, trade_entry_price)
                        risk_per_lot = risk_pts * lot_size
                        lots_by_risk = math.floor(params.discrete_risk_budget / risk_per_lot)
                        lots_by_margin = math.floor(params.capital / margin_per_lot)
                        lots = min(lots_by_risk, lots_by_margin)
                        if lots < 1:
                            lots = 1
                        risk_amount = lots * risk_per_lot
                        pnl_amount = risk_amount * rr_realized
                        pnl_points = rr_realized * risk_pts
                        trade_notes.append(f"lots={lots};margin={lots * margin_per_lot}")
                    elif params.strategy_type in ("Credit Spread", "Debit Spread"):
                        spread_width = params.nifty_spread_width if symbol == "NIFTY" else (params.sensex_spread_width if symbol == "SENSEX" else params.reliance_spread_width)
                        net_risk_pct = (1.0 - params.credit_premium_pct) if params.strategy_type == "Credit Spread" else params.credit_premium_pct
                        net_risk_per_share = net_risk_pct * spread_width
                        risk_per_lot = net_risk_per_share * lot_size
                        lots = math.floor(params.discrete_risk_budget / risk_per_lot)
                        if lots < 1:
                            lots = 1
                        risk_amount = lots * risk_per_lot
                        pnl_amount = risk_amount * rr_realized
                        pnl_points = rr_realized * net_risk_per_share
                        trade_notes.append(f"lots={lots};spread_width={spread_width}")
                    else:  # Naked ATM Buy
                        risk_per_lot = risk_pts * lot_size
                        lots = math.floor(params.discrete_risk_budget / risk_per_lot)
                        if lots < 1:
                            lots = 1
                        risk_amount = lots * risk_per_lot
                        pnl_amount = risk_amount * rr_realized
                        pnl_points = rr_realized * risk_pts
                        trade_notes.append(f"lots={lots}")
                else:
                    pnl_points = rr_realized * risk_pts
                    risk_amount = params.capital * params.risk_per_trade_pct
                    pnl_amount = risk_amount * rr_realized
                
                trade_notes.append(params.strategy_type)
                trade_notes.append(f"trig={params.trigger_type}")
                trade_notes.append(f"sl={params.stop_loss_type}")
                
                trades.append(Trade(
                    symbol=symbol,
                    date=str(merged.iloc[i]["date"]),
                    direction=trade_direction,
                    entry_time=str(merged.iloc[trade_entry_idx]["dt"]),
                    exit_time=str(dt_val),
                    entry_price=round(trade_entry_price, 4),
                    exit_price=round(exit_price, 4),
                    stop_price=round(trade_stop, 4),
                    target_price=round(trade_target, 4),
                    risk_points=round(risk_pts, 4),
                    pnl_points=round(pnl_points, 4),
                    rr_realized=round(rr_realized, 4),
                    risk_amount=round(risk_amount, 2),
                    pnl_amount=round(pnl_amount, 2),
                    score=trade_score,
                    exit_reason=exit_reason,
                    setup_notes=";".join(trade_notes),
                ))
                
                trade_active = False
                pending_pullback = False
 
        # B. Structure invalidation (when no trade is active)
        if not trade_active and pending_pullback:
            close_price = closes[i]
            if pending_direction == "BULLISH":
                if close_price < pending_ob_low:
                    pending_pullback = False
            else: # BEARISH
                if close_price > pending_ob_high:
                    pending_pullback = False
 
        # C. Signal confirmation & entry checks
        if not trade_active and trades_today < params.max_trades_per_day:
            trend_5m = trends_5m[i]
            ob_low = ob_lows_5m[i]
            ob_high = ob_highs_5m[i]
            zone_entry_5m = zone_entries_5m[i]
            stop_5m = stops_5m[i]
            target_5m = targets_5m[i]
            
            # Daily support/resistance boundaries
            pd_low = pd_lows[i]
            pd_high = pd_highs[i]
            
            # Reset pending pullback if trend goes neutral or changes direction
            if trend_5m == 0:
                pending_pullback = False
            elif trend_5m == 1 and pending_direction == "BEARISH":
                pending_pullback = False
            elif trend_5m == -1 and pending_direction == "BULLISH":
                pending_pullback = False
                
            if trend_5m != 0:
                time_str = times[i]
                low_price = lows[i]
                high_price = highs[i]
                close_price = closes[i]
                
                if params.entry_start <= time_str <= params.entry_cutoff:
                    # 1. Look for pullback to Order Block OR Previous Day Low/High to activate pending
                    if not pending_pullback:
                        if trend_5m == 1:
                            # Touch Demand OB or PDL
                            support_level = zone_entry_5m
                            if not pd.isna(pd_low) and pd_low > ob_low:
                                support_level = max(zone_entry_5m, pd_low)
                            if low_price <= support_level:
                                pending_pullback = True
                                pending_direction = "BULLISH"
                                pending_zone_entry = support_level
                                pending_stop_5m = stop_5m
                                pending_target_5m = target_5m
                                pending_ob_low = ob_low
                        elif trend_5m == -1:
                            # Touch Supply OB or PDH
                            resistance_level = zone_entry_5m
                            if not pd.isna(pd_high) and pd_high < ob_high:
                                resistance_level = min(zone_entry_5m, pd_high)
                            if high_price >= resistance_level:
                                pending_pullback = True
                                pending_direction = "BEARISH"
                                pending_zone_entry = resistance_level
                                pending_stop_5m = stop_5m
                                pending_target_5m = target_5m
                                pending_ob_high = ob_high
                            
                    # 2. Monitor pending state for entries
                    if pending_pullback:
                        score_val = 70
                        notes = ["BOS", "OB_mitigation"]
                        
                        # Confluence points
                        if pending_direction == "BULLISH":
                            if not pd.isna(pd_low) and low_price <= pd_low:
                                score_val += 10
                                notes.append("PDL_confluence")
                        else:
                            if not pd.isna(pd_high) and high_price >= pd_high:
                                score_val += 10
                                notes.append("PDH_confluence")
                                
                        # VWAP alignment
                        if (pending_direction == "BULLISH" and close_price > vwaps[i]) or (pending_direction == "BEARISH" and close_price < vwaps[i]):
                            score_val += 10
                            notes.append("vwap_aligned")
                            
                        # Volume expansion
                        v_median = volume_median20[i]
                        if v_median > 0 and volumes[i] >= v_median * 1.15:
                            score_val += 10
                            notes.append("volume_expansion")
                            
                        if pending_direction == "BULLISH":
                            if bull_triggers[i]:
                                if not params.use_vwap_filter or close_price > vwaps[i]:
                                    trade_active = True
                                    trade_entry_idx = i
                                    trade_direction = "BULLISH"
                                    trade_target = pending_target_5m
                                    trade_score = score_val
                                    trade_notes = list(notes)
                                    trades_today += 1
                                    
                                    if params.trigger_type == "none":
                                        trade_entry_price = min(opens[i], pending_zone_entry)
                                    else:
                                        trade_entry_price = close_price
                                    
                                    # Stop Loss logic
                                    if params.stop_loss_type == "5m_origin":
                                        trade_stop = pending_stop_5m
                                    elif params.stop_loss_type == "1m_candle_low":
                                        min_low = min(lows[max(0, i-1):i+1])
                                        fallback_atr = strike_step / 10.0
                                        atr_val = atr14[i] if not math.isnan(atr14[i]) else fallback_atr
                                        trade_stop = min_low - 0.5 * atr_val
                                        trade_stop = max(trade_stop, pending_stop_5m)
                                    else: # "1m_atr_1.5"
                                        fallback_atr = strike_step / 10.0
                                        atr_val = atr14[i] if not math.isnan(atr14[i]) else fallback_atr
                                        trade_stop = trade_entry_price - 1.5 * atr_val
                                        trade_stop = max(trade_stop, pending_stop_5m)
                                    
                                    # Enforce minimum stop-loss distance in points for realistic live trading
                                    min_stop_pts = 15.0 if symbol == "NIFTY" else (30.0 if symbol == "SENSEX" else (3.0 if symbol == "RELIANCE" else 5.0))
                                    if trade_entry_price - trade_stop < min_stop_pts:
                                        trade_stop = trade_entry_price - min_stop_pts
                                        
                                    if trade_entry_price - trade_stop <= 0:
                                        trade_active = False
                                        trades_today -= 1
                                        
                        else: # BEARISH
                            if bear_triggers[i]:
                                if not params.use_vwap_filter or close_price < vwaps[i]:
                                    trade_active = True
                                    trade_entry_idx = i
                                    trade_direction = "BEARISH"
                                    trade_target = pending_target_5m
                                    trade_score = score_val
                                    trade_notes = list(notes)
                                    trades_today += 1
                                    
                                    if params.trigger_type == "none":
                                        trade_entry_price = max(opens[i], pending_zone_entry)
                                    else:
                                        trade_entry_price = close_price
                                    
                                    # Stop Loss logic
                                    if params.stop_loss_type == "5m_origin":
                                        trade_stop = pending_stop_5m
                                    elif params.stop_loss_type == "1m_candle_low":
                                        max_high = max(highs[max(0, i-1):i+1])
                                        fallback_atr = strike_step / 10.0
                                        atr_val = atr14[i] if not math.isnan(atr14[i]) else fallback_atr
                                        trade_stop = max_high + 0.5 * atr_val
                                        trade_stop = min(trade_stop, pending_stop_5m)
                                    else: # "1m_atr_1.5"
                                        fallback_atr = strike_step / 10.0
                                        atr_val = atr14[i] if not math.isnan(atr14[i]) else fallback_atr
                                        trade_stop = trade_entry_price + 1.5 * atr_val
                                        trade_stop = min(trade_stop, pending_stop_5m)
                                    
                                    # Enforce minimum stop-loss distance in points for realistic live trading
                                    min_stop_pts = 15.0 if symbol == "NIFTY" else (30.0 if symbol == "SENSEX" else (3.0 if symbol == "RELIANCE" else 5.0))
                                    if trade_stop - trade_entry_price < min_stop_pts:
                                        trade_stop = trade_entry_price + min_stop_pts
                                        
                                    if trade_stop - trade_entry_price <= 0:
                                        trade_active = False
                                        trades_today -= 1
 
    return trades


def max_drawdown(values: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    return drawdown


def summarize(symbol: str, trades: list[Trade]) -> dict:
    if not trades:
        return {
            "symbol": symbol,
            "trades": 0,
            "win_rate": 0.0,
            "avg_rr": 0.0,
            "expectancy_rr": 0.0,
            "total_rr": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_rr": 0.0,
            "best_rr": 0.0,
            "worst_rr": 0.0,
        }
    rrs = [float(t.rr_realized) for t in trades]
    wins = [rr for rr in rrs if rr > 0]
    losses = [rr for rr in rrs if rr <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "symbol": symbol,
        "trades": len(trades),
        "win_rate": round(len(wins) / len(trades), 4),
        "avg_rr": round(sum(rrs) / len(rrs), 4),
        "expectancy_rr": round(sum(rrs) / len(rrs), 4),
        "total_rr": round(sum(rrs), 4),
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss else (999.0 if gross_profit else 0.0),
        "max_drawdown_rr": round(max_drawdown(rrs), 4),
        "best_rr": round(max(rrs), 4),
        "worst_rr": round(min(rrs), 4),
    }


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    except PermissionError:
        backup_path = path.parent.parent / "backtests_temp" / path.name
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        with backup_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        logging.info("Wrote backup file to %s due to permission error", backup_path)


def run_grid_optimization(selected: list[dict], args: argparse.Namespace, output_dir: Path):
    logging.info("Starting MTF Order Block & SFP Grid Search optimization on %d symbols...", len(selected))
    
    triggers = ["none", "ma_crossover", "sfp", "choch"]
    stop_losses = ["5m_origin", "1m_candle_low", "1m_atr_1.5"]
    strategies = ["Naked ATM Buy", "Debit Spread", "Credit Spread", "Ratio", "Synthetic Future"]
    
    # Pre-load candles
    input_dir = Path(args.input_dir)
    data_dict = {}
    
    for instrument in selected:
        sym = instrument["symbol"]
        path_5m = input_dir / f"{sym}_5m.csv"
        path_1m = input_dir / f"{sym}_1m.csv"
        if path_5m.exists() and path_1m.exists():
            df_5m = load_candles(path_5m)
            df_1m = load_candles(path_1m)
            
            # Align dates
            min_date = df_1m["date"].min()
            df_5m_filtered = df_5m[df_5m["date"] >= min_date].copy()
            df_1m_filtered = df_1m[df_1m["date"] >= df_5m_filtered["date"].min()].copy()
            data_dict[sym] = (df_5m_filtered, df_1m_filtered)
            
    opt_rows = []
    total_runs = len(triggers) * len(stop_losses) * len(strategies)
    run_idx = 0
    
    for trig, sl_type, strat in itertools.product(triggers, stop_losses, strategies):
        run_idx += 1
        
        params = BacktestParams(
            strategy_type=strat,
            trigger_type=trig,
            stop_loss_type=sl_type,
            pivot_len=5,
            entry_retracement=0.618,
            target_type="extreme",
            discrete_risk_budget=args.discrete_risk_budget,
            capital=200000.0,
            max_trades_per_day=args.max_trades_per_day,
            use_vwap_filter=not args.disable_vwap_filter
        )
        
        all_trades = []
        for sym, (df_5m_f, df_1m_f) in data_dict.items():
            trades = backtest_ob_mitigation(sym, df_5m_f, df_1m_f, params)
            all_trades.extend(trades)
            
        summary = summarize("PORTFOLIO", all_trades)
        
        opt_rows.append({
            "trigger_type": trig,
            "stop_loss_type": sl_type,
            "strategy_type": strat,
            "trades": summary["trades"],
            "win_rate": summary["win_rate"],
            "expectancy_rr": summary["expectancy_rr"],
            "total_rr": summary["total_rr"],
            "max_drawdown_rr": summary["max_drawdown_rr"]
        })
        
        if run_idx % 10 == 0 or run_idx == total_runs:
            logging.info(
                "[%d/%d] Trig=%s, SL=%s, Strat=%s -> Trades=%d, WR=%.2f%%, Expectancy=%.2fR",
                run_idx, total_runs, trig, sl_type, strat,
                summary["trades"], summary["win_rate"] * 100, summary["expectancy_rr"]
            )
            
    opt_rows = sorted(opt_rows, key=lambda x: x["expectancy_rr"], reverse=True)
    write_csv(
        output_dir / "ob_mitigation_optimization.csv",
        opt_rows,
        ["trigger_type", "stop_loss_type", "strategy_type", "trades", "win_rate", "expectancy_rr", "total_rr", "max_drawdown_rr"]
    )
    logging.info("Grid results written to %s", output_dir / "ob_mitigation_optimization.csv")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest MTF Order Block & SFP Strategy.")
    parser.add_argument("--symbols", help="Comma-separated symbols. Default: NIFTY,SENSEX,RELIANCE.")
    parser.add_argument("--universe-file", help="Optional JSON universe override.")
    parser.add_argument("--batch", type=int, default=1, help="1-based batch number to run.")
    parser.add_argument("--batch-size", type=int, default=5, help="Symbols per execution.")
    parser.add_argument("--input-dir", default="data/historical", help="Directory containing CSV files.")
    parser.add_argument("--output-dir", default="data/backtests", help="Directory to write reports.")
    parser.add_argument("--optimize", action="store_true", help="Run parameters grid search.")
    parser.add_argument("--trigger", default="choch", choices=["none", "ma_crossover", "sfp", "choch"])
    parser.add_argument("--stop-loss", default="5m_origin", choices=["5m_origin", "1m_candle_low", "1m_atr_1.5"])
    parser.add_argument("--strategy", default="Ratio", choices=["Naked ATM Buy", "Debit Spread", "Credit Spread", "Ratio", "Synthetic Future"])
    parser.add_argument("--discrete-risk-budget", type=float, default=1000.0)
    parser.add_argument("--max-trades-per-day", type=int, default=3)
    parser.add_argument("--disable-vwap-filter", action="store_true", help="Disable VWAP trend filter.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    setup_logging(output_dir)
    
    symbols_filter = args.symbols if args.symbols else "NIFTY,SENSEX,RELIANCE"
    instruments = select_universe(symbols_filter, args.universe_file)
    selected, total_batches = batch_universe(instruments, batch=args.batch, batch_size=args.batch_size)
    
    logging.info(
        "Selected batch %s/%s with %s instruments: %s",
        args.batch,
        total_batches,
        len(selected),
        ", ".join(item["symbol"] for item in selected),
    )
    
    if args.optimize:
        run_grid_optimization(selected, args, output_dir)
        return 0
        
    params = BacktestParams(
        strategy_type=args.strategy,
        trigger_type=args.trigger,
        stop_loss_type=args.stop_loss,
        pivot_len=5,
        entry_retracement=0.618,
        target_type="extreme",
        discrete_risk_budget=args.discrete_risk_budget,
        capital=200000.0,
        max_trades_per_day=args.max_trades_per_day,
        use_vwap_filter=not args.disable_vwap_filter
    )
    
    input_dir = Path(args.input_dir)
    all_trades = []
    summary_rows = []
    
    for instrument in selected:
        sym = instrument["symbol"]
        path_5m = input_dir / f"{sym}_5m.csv"
        path_1m = input_dir / f"{sym}_1m.csv"
        
        if not path_5m.exists() or not path_1m.exists():
            logging.warning("[%s] missing 5m or 1m data file. Skipping.", sym)
            continue
            
        df_5m = load_candles(path_5m)
        df_1m = load_candles(path_1m)
        
        # Align date bounds
        df_5m_filtered = df_5m[df_5m["date"] >= df_1m["date"].min()].copy()
        df_1m_filtered = df_1m[df_1m["date"] >= df_5m_filtered["date"].min()].copy()
        
        trades = backtest_ob_mitigation(sym, df_5m_filtered, df_1m_filtered, params)
        all_trades.extend(trades)
        row = summarize(sym, trades)
        summary_rows.append(row)
        
    trade_rows = [asdict(t) for t in all_trades]
    write_csv(output_dir / "ob_mitigation_trades.csv", trade_rows, TRADE_FIELDS)
    write_csv(output_dir / "ob_mitigation_summary.csv", summary_rows, ["symbol", "trades", "win_rate", "avg_rr", "expectancy_rr", "total_rr", "profit_factor", "max_drawdown_rr", "best_rr", "worst_rr"])
    
    portfolio = summarize("PORTFOLIO", all_trades)
    logging.info(
        "OB Mitigation Portfolio Summary: trades=%d win_rate=%.2f%% expectancy=%.2fR total=%.2fR max_dd=%.2fR",
        portfolio["trades"], portfolio["win_rate"] * 100, portfolio["expectancy_rr"],
        portfolio["total_rr"], portfolio["max_drawdown_rr"]
    )
    logging.info("Reports written to %s", output_dir)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
