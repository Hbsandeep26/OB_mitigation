"""Backtest Confirmed Fibonacci Pullback strategy on options.

Tracks swing pivots (pivots highs/lows), Break of Structure (BOS), and 
Fibonacci Golden Zone retracements to trigger high-expectancy entries.
Supports Naked Buy, Debit Spread, Credit Spread, and 1x2 Ratio Spread options.
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
    target_type: str = "extreme"  # "extreme", "extension_0272", "rr_ratio"
    target_rr: float = 2.0
    capital: float = 200000.0
    risk_per_trade_pct: float = 0.005
    entry_start: str = "09:30"
    entry_cutoff: str = "13:45"
    exit_time: str = "15:00"
    strategy_type: str = "Debit Spread"  # "Naked ATM Buy", "Debit Spread", "Credit Spread", "Ratio"
    credit_premium_pct: float = 0.50
    discrete_risk_budget: float | None = 300.0
    nifty_spread_width: float = 10.0
    sensex_spread_width: float = 20.0
    reliance_spread_width: float = 2.0


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
            logging.FileHandler(output_dir / "backtest_fibonacci_pullback.log", encoding="utf-8"),
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
        # Capped profit at the target ratio, linear return in between
        if exit_reason == "TARGET":
            return rr_target
        if direction == "BULLISH":
            r_linear = (exit_price - entry_price) / risk
        else:
            r_linear = (entry_price - exit_price) / risk
            
        return min(rr_target, max(-1.0, r_linear))

    elif strategy_type == "Credit Spread":
        # Max profit relative to risk = premium_pct / (1 - premium_pct)
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
        # 1x2 Ratio Spread Settlement Model
        if exit_reason == "TARGET":
            return 2.5
        if exit_reason == "STOP":
            return -1.0
        if exit_reason in ("TIME_EXIT", "NO_EXIT_BARS"):
            k_long = entry_price
            if direction == "BULLISH":
                k_short = k_long + strike_step
                if exit_price <= k_long:
                    return 0.25
                elif exit_price <= k_short:
                    return 0.25 + 2.25 * ((exit_price - k_long) / strike_step)
                else:
                    return max(-1.0, 2.5 - 2.25 * ((exit_price - k_short) / strike_step))
            else:  # BEARISH
                k_short = k_long - strike_step
                if exit_price >= k_long:
                    return 0.25
                elif exit_price >= k_short:
                    return 0.25 + 2.25 * ((k_long - exit_price) / strike_step)
                else:
                    return max(-1.0, 2.5 - 2.25 * ((k_short - exit_price) / strike_step))

    return 0.0


def calculate_pivots(df: pd.DataFrame, L: int) -> tuple[list[float | None], list[int | None], list[float | None], list[int | None]]:
    """Calculate confirmed pivot highs and pivot lows.
    
    A candle at index i-L is a pivot high/low if it is the extreme in the 
    window [i-2*L, i]. The confirmation happens at index i.
    
    Returns lists of the same length as df:
        pivot_hi_vals, pivot_hi_idxs, pivot_lo_vals, pivot_lo_idxs
    """
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
        
        # Check pivot high
        is_hi = True
        val_hi = highs[target_idx]
        for j in range(target_idx - L, target_idx + L + 1):
            if highs[j] > val_hi:
                is_hi = False
                break
        if is_hi:
            last_hi_val = val_hi
            last_hi_idx = target_idx
            
        # Check pivot low
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


def backtest_symbol(symbol: str, candles: pd.DataFrame, params: BacktestParams) -> list[Trade]:
    trades = []
    if candles.empty:
        return trades

    # Pre-calculate pivots and properties
    L = params.pivot_len
    pivot_hi_vals, pivot_hi_idxs, pivot_lo_vals, pivot_lo_idxs = calculate_pivots(candles, L)
    
    # State variables
    trend = 0  # 0 = Neutral, 1 = Bullish Leg, -1 = Bearish Leg
    anchor_hi, anchor_lo = None, None
    anchor_hi_idx, anchor_lo_idx = None, None
    leg_age = 0
    stop_loss = None
    take_profit = None
    zone_entry_price = None
    
    trade_active = False
    trade_entry_idx = None
    trade_entry_price = None
    trade_direction = None
    trade_stop = None
    trade_target = None
    trade_score = 0
    trade_notes = []
    
    highs = candles["high"].values
    lows = candles["low"].values
    closes = candles["close"].values
    opens = candles["open"].values
    times = candles["time"].values
    dts = candles["dt"].values
    atr14 = candles["atr14"].values
    vwaps = candles["vwap"].values
    volumes = candles["volume"].values
    volume_median20 = candles["volume_median20"].values
    
    strike_step = get_strike_step(symbol)
    
    n = len(candles)
    for i in range(2 * L, n):
        # 1. Trade Exit Monitoring
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
                    exit_price = open_price if open_price <= trade_target else trade_target
                    exit_reason = "TARGET"
                    exit_dt = dt_val
                elif time_str >= params.exit_time:
                    exit_price = close_price
                    exit_reason = "TIME_EXIT"
                    exit_dt = dt_val
                    
            if exit_reason is not None:
                # Calculate R-multiple
                bars_elapsed = i - trade_entry_idx
                # Estimate total possible bars in a day exit window (roughly 75 bars on 5m)
                total_bars = 75
                
                # Determine R:R target for spread cap
                realized_rr_target = params.target_rr
                if params.target_type == "extreme":
                    # Underlying R:R target is (anchor_hi - entry) / (entry - stop)
                    underlying_risk = abs(trade_entry_price - trade_stop)
                    underlying_reward = abs(trade_target - trade_entry_price)
                    realized_rr_target = underlying_reward / underlying_risk if underlying_risk > 0 else 1.0
                elif params.target_type == "extension_0272":
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
                
                # Sizing Calculations
                risk_pts = abs(trade_entry_price - trade_stop)
                lot_size = 65 if symbol == "NIFTY" else (20 if symbol == "SENSEX" else 1)
                
                if params.discrete_risk_budget is not None and params.discrete_risk_budget > 0:
                    if params.strategy_type == "Ratio":
                        margin_per_lot = get_margin_per_lot(symbol, trade_entry_price)
                        risk_budget_rupees = params.capital * 0.005  # 0.5% portfolio risk
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
                    elif params.strategy_type == "Credit Spread":
                        spread_width = params.nifty_spread_width if symbol == "NIFTY" else (params.sensex_spread_width if symbol == "SENSEX" else params.reliance_spread_width)
                        net_risk_per_share = (1.0 - params.credit_premium_pct) * spread_width
                        risk_per_lot = net_risk_per_share * lot_size
                        lots = math.floor(params.discrete_risk_budget / risk_per_lot)
                        if lots < 1:
                            lots = 1
                        risk_amount = lots * risk_per_lot
                        pnl_amount = risk_amount * rr_realized
                        pnl_points = rr_realized * net_risk_per_share
                        trade_notes.append(f"lots={lots};spread_width={spread_width}")
                    elif params.strategy_type == "Debit Spread":
                        spread_width = params.nifty_spread_width if symbol == "NIFTY" else (params.sensex_spread_width if symbol == "SENSEX" else params.reliance_spread_width)
                        # Debit premium cost is credit_premium_pct * width (standard replication)
                        net_risk_per_share = params.credit_premium_pct * spread_width
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
                
                trades.append(Trade(
                    symbol=symbol,
                    date=str(candles.iloc[i]["date"]),
                    direction=trade_direction,
                    entry_time=str(candles.iloc[trade_entry_idx]["dt"]),
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
                
                # Reset states
                trade_active = False
                trend = 0  # Look for a new leg

        # 2. Leg invalidation and expiration
        if not trade_active and trend != 0:
            close_price = closes[i]
            if trend == 1:
                if close_price < stop_loss:
                    trend = 0  # Invalidation
                elif leg_age > params.max_leg_age:
                    trend = 0  # Stale structure
                else:
                    leg_age += 1
            elif trend == -1:
                if close_price > stop_loss:
                    trend = 0  # Invalidation
                elif leg_age > params.max_leg_age:
                    trend = 0  # Stale structure
                else:
                    leg_age += 1

        # 3. Pivot confirmations & structure break check
        if not trade_active:
            close_price = closes[i]
            high_price = highs[i]
            low_price = lows[i]
            
            # Retrieve last confirmed pivots from pre-computed values
            last_hi_val = pivot_hi_vals[i]
            last_hi_idx = pivot_hi_idxs[i]
            last_lo_val = pivot_lo_vals[i]
            last_lo_idx = pivot_lo_idxs[i]
            
            # Check for BULLISH Break of Structure (BOS)
            if last_hi_val is not None and close_price > last_hi_val:
                # Origin low is the minimum low between the pivot high's bar index and current bar
                start_search = last_hi_idx
                origin_low_val = min(lows[start_search:i+1])
                origin_low_idx_offset = lows[start_search:i+1].argmin()
                origin_low_idx = start_search + origin_low_idx_offset
                
                # Impulse high is the maximum high reached between the origin low and the current bar
                impulse_high_val = max(highs[origin_low_idx:i+1])
                impulse_high_idx_offset = highs[origin_low_idx:i+1].argmax()
                impulse_high_idx = origin_low_idx + impulse_high_idx_offset
                
                leg_size = impulse_high_val - origin_low_val
                curr_atr = atr14[i] if not math.isnan(atr14[i]) else 1.0
                
                if leg_size >= params.displacement_multiplier * curr_atr:
                    trend = 1
                    anchor_hi = impulse_high_val
                    anchor_lo = origin_low_val
                    anchor_hi_idx = impulse_high_idx
                    anchor_lo_idx = origin_low_idx
                    leg_age = 0
                    
                    # Calculate Golden Zone entry level
                    zone_entry_price = anchor_hi - params.entry_retracement * (anchor_hi - anchor_lo)
                    stop_loss = anchor_lo - params.invalidation_buffer * curr_atr
                    
                    # Set take-profit based on target model
                    if params.target_type == "extreme":
                        take_profit = anchor_hi
                    elif params.target_type == "extension_0272":
                        take_profit = anchor_hi + 0.272 * (anchor_hi - anchor_lo)
                    else:  # rr_ratio
                        underlying_risk = abs(zone_entry_price - stop_loss)
                        take_profit = zone_entry_price + params.target_rr * underlying_risk

            # Check for BEARISH Break of Structure (BOS)
            elif last_lo_val is not None and close_price < last_lo_val:
                # Origin high is the maximum high between the pivot low's bar index and current bar
                start_search = last_lo_idx
                origin_high_val = max(highs[start_search:i+1])
                origin_high_idx_offset = highs[start_search:i+1].argmax()
                origin_high_idx = start_search + origin_high_idx_offset
                
                # Impulse low is the minimum low reached between the origin high and the current bar
                impulse_low_val = min(lows[origin_high_idx:i+1])
                impulse_low_idx_offset = lows[origin_high_idx:i+1].argmin()
                impulse_low_idx = origin_high_idx + impulse_low_idx_offset
                
                leg_size = origin_high_val - impulse_low_val
                curr_atr = atr14[i] if not math.isnan(atr14[i]) else 1.0
                
                if leg_size >= params.displacement_multiplier * curr_atr:
                    trend = -1
                    anchor_hi = origin_high_val
                    anchor_lo = impulse_low_val
                    anchor_hi_idx = origin_high_idx
                    anchor_lo_idx = impulse_low_idx
                    leg_age = 0
                    
                    # Calculate Golden Zone entry level
                    zone_entry_price = anchor_lo + params.entry_retracement * (anchor_hi - anchor_lo)
                    stop_loss = anchor_hi + params.invalidation_buffer * curr_atr
                    
                    # Set take-profit based on target model
                    if params.target_type == "extreme":
                        take_profit = anchor_lo
                    elif params.target_type == "extension_0272":
                        take_profit = anchor_lo - 0.272 * (anchor_hi - anchor_lo)
                    else:  # rr_ratio
                        underlying_risk = abs(stop_loss - zone_entry_price)
                        take_profit = zone_entry_price - params.target_rr * underlying_risk

        # 4. Entry Fill Monitoring
        if not trade_active and trend != 0:
            time_str = times[i]
            # Check if within daily entry window
            if params.entry_start <= time_str <= params.entry_cutoff:
                high_price = highs[i]
                low_price = lows[i]
                open_price = opens[i]
                close_price = closes[i]
                
                score_val = 60  # Base score for Confirmed Fib Pullback setup
                notes = ["BOS", "fib_golden_zone"]
                
                # VWAP confirmation
                if (trend == 1 and close_price > vwaps[i]) or (trend == -1 and close_price < vwaps[i]):
                    score_val += 10
                    notes.append("vwap_aligned")
                
                # Volume confirmation
                v_median = volume_median20[i]
                if v_median > 0 and volumes[i] >= v_median * 1.15:
                    score_val += 10
                    notes.append("volume_expansion")
                    
                # Greeks ATR filter
                curr_atr = atr14[i]
                atr_pct = curr_atr / close_price if close_price > 0 else 0.0
                if 0.0005 <= atr_pct <= 0.018:
                    score_val += 20
                    notes.append("greeks_iv_proxy_ok")
                
                if trend == 1:
                    # Check if price pulls back to our entry order
                    if low_price <= zone_entry_price:
                        # Entry fill!
                        trade_active = True
                        trade_entry_idx = i
                        # Fill at open if open is already below zone entry, else fill at limit price
                        trade_entry_price = min(open_price, zone_entry_price)
                        trade_direction = "BULLISH"
                        trade_stop = stop_loss
                        trade_target = take_profit
                        trade_score = score_val
                        trade_notes = list(notes)
                else: # trend == -1
                    if high_price >= zone_entry_price:
                        # Entry fill!
                        trade_active = True
                        trade_entry_idx = i
                        # Fill at open if open is already above zone entry, else fill at limit price
                        trade_entry_price = max(open_price, zone_entry_price)
                        trade_direction = "BEARISH"
                        trade_stop = stop_loss
                        trade_target = take_profit
                        trade_score = score_val
                        trade_notes = list(notes)

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


def run_for_params(selected: list[dict], args: argparse.Namespace, params: BacktestParams):
    input_dir = Path(args.input_dir)
    all_trades = []
    summary_rows = []

    for instrument in selected:
        symbol = instrument["symbol"]
        path = input_dir / f"{symbol}_{args.interval}m.csv"
        if not path.exists():
            summary_rows.append(summarize(symbol, []))
            continue
        candles = load_candles(path)
        trades = backtest_symbol(symbol, candles, params)
        all_trades.extend(trades)
        row = summarize(symbol, trades)
        summary_rows.append(row)
    return all_trades, summary_rows


def optimization_grid(selected: list[dict], args: argparse.Namespace, output_dir: Path):
    logging.info("Starting grid search optimization for Confirmed Fibonacci Pullback strategy...")
    
    # We will test combinations of:
    # 1. strategy_type: ATM Option Buy, Debit Spread, Credit Spread, Ratio Spread
    # 2. pivot_len: 5, 8
    # 3. entry_retracement: 0.618, 0.705, 0.786
    # 4. target_type: extreme, extension_0272, rr_ratio (2.0R, 3.0R)
    
    strategies = ["Naked ATM Buy", "Debit Spread", "Credit Spread", "Ratio"]
    pivot_lens = [5, 8]
    retracements = [0.618, 0.705, 0.786]
    targets = [
        ("extreme", 0.0),
        ("extension_0272", 0.0),
        ("rr_ratio", 2.0),
        ("rr_ratio", 3.0)
    ]
    
    opt_rows = []
    
    # Pre-load candles to avoid reloading in the loops
    input_dir = Path(args.input_dir)
    candles_dict = {}
    for instrument in selected:
        symbol = instrument["symbol"]
        path = input_dir / f"{symbol}_{args.interval}m.csv"
        if path.exists():
            candles_dict[symbol] = load_candles(path)
            
    total_runs = len(strategies) * len(pivot_lens) * len(retracements) * len(targets)
    run_idx = 0
    
    for strat, pl, ret, (tgt_type, tgt_rr) in itertools.product(strategies, pivot_lens, retracements, targets):
        run_idx += 1
        
        params = BacktestParams(
            strategy_type=strat,
            pivot_len=pl,
            entry_retracement=ret,
            target_type=tgt_type,
            target_rr=tgt_rr,
            credit_premium_pct=0.50 if strat != "Credit Spread" else 0.50, # ATM credit spreads
            discrete_risk_budget=300.0,
            capital=200000.0
        )
        
        all_trades = []
        for symbol, candles in candles_dict.items():
            trades = backtest_symbol(symbol, candles, params)
            all_trades.extend(trades)
            
        summary = summarize("PORTFOLIO", all_trades)
        
        opt_rows.append({
            "strategy_type": strat,
            "pivot_len": pl,
            "entry_retracement": ret,
            "target_type": tgt_type,
            "target_rr": tgt_rr if tgt_type == "rr_ratio" else 0.0,
            "trades": summary["trades"],
            "win_rate": summary["win_rate"],
            "expectancy_rr": summary["expectancy_rr"],
            "total_rr": summary["total_rr"],
            "max_drawdown_rr": summary["max_drawdown_rr"]
        })
        
        if run_idx % 10 == 0 or run_idx == total_runs:
            logging.info(
                "[%d/%d] Strategy=%s, Pivot=%d, Retracement=%.3f, Target=%s -> Trades=%d, WR=%.2f%%, Expectancy=%.2fR",
                run_idx, total_runs, strat, pl, ret, tgt_type,
                summary["trades"], summary["win_rate"] * 100, summary["expectancy_rr"]
            )
            
    # Write optimization results
    opt_rows = sorted(opt_rows, key=lambda x: x["expectancy_rr"], reverse=True)
    write_csv(
        output_dir / "fib_pullback_optimization.csv",
        opt_rows,
        ["strategy_type", "pivot_len", "entry_retracement", "target_type", "target_rr", "trades", "win_rate", "expectancy_rr", "total_rr", "max_drawdown_rr"]
    )
    logging.info("Optimization report written to %s", output_dir / "fib_pullback_optimization.csv")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest Confirmed Fibonacci Pullback Options Strategy.")
    parser.add_argument("--symbols", help="Comma-separated symbols. Default: NIFTY, SENSEX, RELIANCE.")
    parser.add_argument("--universe-file", help="Optional JSON universe override.")
    parser.add_argument("--batch", type=int, default=1, help="1-based batch number to run.")
    parser.add_argument("--batch-size", type=int, default=5, help="Symbols per execution.")
    parser.add_argument("--input-dir", default="data/historical", help="Directory with *_5m.csv files.")
    parser.add_argument("--output-dir", default="data/backtests", help="Directory for backtest reports.")
    parser.add_argument("--interval", type=int, default=5, help="CSV interval suffix.")
    parser.add_argument("--optimize", action="store_true", help="Run parameters optimization grid search.")
    parser.add_argument("--strategy", default="Debit Spread", choices=["Naked ATM Buy", "Debit Spread", "Credit Spread", "Ratio"], help="Options strategy to backtest.")
    parser.add_argument("--pivot-len", type=int, default=5, help="Swing pivot lookback length.")
    parser.add_argument("--retracement", type=float, default=0.618, help="Fib entry retracement ratio.")
    parser.add_argument("--target-type", default="extreme", choices=["extreme", "extension_0272", "rr_ratio"])
    parser.add_argument("--target-rr", type=float, default=2.0, help="Fixed target RR if rr_ratio target type.")
    parser.add_argument("--discrete-risk-budget", type=float, default=300.0, help="Fixed budget risk in rupees.")
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
        optimization_grid(selected, args, output_dir)
        return 0

    params = BacktestParams(
        strategy_type=args.strategy,
        pivot_len=args.pivot_len,
        entry_retracement=args.retracement,
        target_type=args.target_type,
        target_rr=args.target_rr,
        discrete_risk_budget=args.discrete_risk_budget,
        capital=200000.0
    )
    
    trades, summary_rows = run_for_params(selected, args, params)
    trade_rows = [asdict(trade) for trade in trades]
    
    write_csv(output_dir / "fib_pullback_trades.csv", trade_rows, TRADE_FIELDS)
    write_csv(output_dir / "fib_pullback_summary.csv", summary_rows, ["symbol", "trades", "win_rate", "avg_rr", "expectancy_rr", "total_rr", "profit_factor", "max_drawdown_rr", "best_rr", "worst_rr"])
    
    portfolio = summarize("PORTFOLIO", trades)
    logging.info(
        "Portfolio: trades=%s win_rate=%.2f%% expectancy=%.2fR total=%.2fR max_dd=%.2fR",
        portfolio["trades"],
        portfolio["win_rate"] * 100,
        portfolio["expectancy_rr"],
        portfolio["total_rr"],
        portfolio["max_drawdown_rr"],
    )
    logging.info("Backtest reports written to %s", output_dir)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
