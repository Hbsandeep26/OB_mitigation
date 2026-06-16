"""Backtest liquidity-sweep debit-spread signals on fetched candle data.

This is a research proxy for the live options strategy. It tests whether the
underlying delivered enough clean directional movement after sweep + BOS. The
actual option-chain OI/Greeks layer should be forward-collected before using
the result for live sizing decisions.
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
    min_score: int = 75
    rr_target: float = 0.75
    risk_per_trade_pct: float = 0.0025
    capital: float = 200000.0
    opening_range_minutes: int = 15
    entry_start: str = "09:30"
    entry_cutoff: str = "13:45"
    exit_time: str = "15:00"
    swing_lookback: int = 5
    volume_multiplier: float = 1.15
    sweep_buffer_pct: float = 0.00005
    max_atr_pct: float = 0.018
    oi_mode: str = "proxy"
    credit_premium_pct: float = 0.9090909090909091
    discrete_risk_budget: float | None = 300.0
    nifty_spread_width: float = 10.0
    sensex_spread_width: float = 20.0
    reliance_spread_width: float = 2.0
    strategy_type: str = "Credit"


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
            logging.FileHandler(output_dir / "backtest_liquidity_sweep.log", encoding="utf-8"),
        ],
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest liquidity-sweep debit-spread proxy.")
    parser.add_argument("--symbols", help="Comma-separated symbols. Default: full liquid universe.")
    parser.add_argument("--universe-file", help="Optional JSON universe override.")
    parser.add_argument("--batch", type=int, default=1, help="1-based batch number to run.")
    parser.add_argument("--batch-size", type=int, default=5, help="Symbols per execution.")
    parser.add_argument("--input-dir", default="data/historical", help="Directory with *_5m.csv files.")
    parser.add_argument("--output-dir", default="data/backtests", help="Directory for backtest reports.")
    parser.add_argument("--interval", type=int, default=5, help="CSV interval suffix, e.g. 5 for SYMBOL_5m.csv.")
    parser.add_argument("--min-score", type=int, default=75)
    parser.add_argument("--rr-target", type=float, default=0.75)
    parser.add_argument("--capital", type=float, default=200000.0)
    parser.add_argument("--risk-per-trade-pct", type=float, default=0.005)
    parser.add_argument("--oi-mode", choices=["proxy", "strict"], default="proxy")
    parser.add_argument("--swing-lookback", type=int, default=5, help="Swing high/low lookback window for BOS.")
    parser.add_argument("--optimize", action="store_true", help="Run a small score/RR grid.")
    parser.add_argument("--credit-premium-pct", type=float, default=0.9090909090909091, help="Options credit spread premium ratio.")
    parser.add_argument("--discrete-risk-budget", type=float, default=300.0, help="If set, enables discrete lot sizing using a fixed risk budget in rupees.")
    parser.add_argument("--nifty-spread-width", type=float, default=10.0, help="Options spread width in points for NIFTY.")
    parser.add_argument("--sensex-spread-width", type=float, default=20.0, help="Options spread width in points for SENSEX.")
    parser.add_argument("--reliance-spread-width", type=float, default=2.0, help="Options spread width in points for RELIANCE.")
    parser.add_argument("--strategy", choices=["Credit", "Ratio"], default="Credit", help="Strategy type to backtest: 'Credit' or 'Ratio'.")
    return parser.parse_args()


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
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    df["pv"] = typical * df["volume"].clip(lower=1.0)
    df["day_cum_pv"] = df.groupby("date")["pv"].cumsum()
    df["day_cum_volume"] = df.groupby("date")["volume"].transform(lambda s: s.clip(lower=1.0).cumsum())
    df["vwap"] = df["day_cum_pv"] / df["day_cum_volume"]
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr14"] = tr.rolling(14, min_periods=5).mean()
    df["volume_median20"] = df["volume"].rolling(20, min_periods=5).median()
    return df


def prior_day_levels(day_df: pd.DataFrame) -> dict:
    return {
        "pdh": float(day_df["high"].max()),
        "pdl": float(day_df["low"].min()),
        "pdc": float(day_df["close"].iloc[-1]),
    }


def score_setup(row, direction: str, bos: bool, swept: bool, volume_ok: bool, atr_ok: bool, params: BacktestParams) -> tuple[int, list[str]]:
    close = float(row["close"])
    vwap = float(row["vwap"])
    notes = []
    score = 0

    if bos:
        score += 20
        notes.append("BOS")
    if swept:
        score += 20
        notes.append("liquidity_sweep")
    if (direction == "BULLISH" and close > vwap) or (direction == "BEARISH" and close < vwap):
        score += 10
        notes.append("vwap_aligned")
    if params.oi_mode == "proxy":
        score += 20 if volume_ok else 10
        notes.append("oi_proxy_volume" if volume_ok else "oi_proxy_partial")
    else:
        notes.append("oi_strict_unavailable")
    if volume_ok:
        score += 10
        notes.append("volume_expansion")
    if atr_ok:
        score += 10
        notes.append("greeks_iv_proxy_ok")
    premium_ok = (
        (direction == "BULLISH" and close >= float(row["open"]))
        or (direction == "BEARISH" and close <= float(row["open"]))
    )
    if premium_ok:
        score += 10
        notes.append("premium_momentum_proxy")
    return min(score, 100), notes


def simulate_exit(day_df: pd.DataFrame, start_idx: int, direction: str, entry: float, stop: float, target: float, params: BacktestParams):
    after = day_df.iloc[start_idx + 1:].copy()
    after = after[after["time"] <= params.exit_time]
    if after.empty:
        return entry, day_df.iloc[start_idx]["dt"], "NO_EXIT_BARS", 0, 1

    total_bars = len(after)
    initial_risk = entry - stop if direction == "BULLISH" else stop - entry
    trailing_activated = False

    for bar_idx, (_, row) in enumerate(after.iterrows()):
        low = float(row["low"])
        high = float(row["high"])

        # Check if profit threshold of +0.75R has been reached to activate trailing stop
        if not trailing_activated and initial_risk > 0:
            if direction == "BULLISH":
                floating_r = (high - entry) / initial_risk
            else:
                floating_r = (entry - low) / initial_risk
            if floating_r >= 0.75:
                trailing_activated = True

        # Trailing stop: if trade has been open for more than 18 bars (90 minutes on a 5-minute chart)
        # AND the profit gate has been unlocked
        if bar_idx > 18 and trailing_activated:
            current_vwap = float(row["vwap"])
            if direction == "BULLISH" and current_vwap > stop:
                stop = current_vwap  # Lock in profits using trailing VWAP
            elif direction == "BEARISH" and current_vwap < stop:
                stop = current_vwap

        if direction == "BULLISH":
            if low <= stop:
                return stop, row["dt"], "STOP", bar_idx + 1, total_bars
            if high >= target:
                return target, row["dt"], "TARGET", bar_idx + 1, total_bars
        else:
            if high >= stop:
                return stop, row["dt"], "STOP", bar_idx + 1, total_bars
            if low <= target:
                return target, row["dt"], "TARGET", bar_idx + 1, total_bars

    last = after.iloc[-1]
    return float(last["close"]), last["dt"], "TIME_EXIT", total_bars, total_bars


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
    credit_premium_pct: float = 0.70,
    strike_step: float = 50.0
) -> float:
    """Calculate the realized R-multiple based on Strategy Type (Credit/Debit/Ratio) and Exit Conditions."""
    # Calculate risk
    risk = entry_price - original_stop if direction == "BULLISH" else original_stop - entry_price
    if risk <= 0:
        return -1.0

    if strategy_type == "Ratio":
        if exit_reason == "TARGET":
            return 2.5
        if exit_reason == "STOP":
            return -1.0
        if exit_reason in ("TIME_EXIT", "NO_EXIT_BARS"):
            # 1x2 Ratio Spread expiration payoff model
            k_long = entry_price
            if direction == "BULLISH":
                k_short = k_long + strike_step
                if exit_price <= k_long:
                    return 0.25
                elif exit_price <= k_short:
                    return 0.25 + 2.25 * ((exit_price - k_long) / strike_step)
                else:
                    return max(-1.0, 2.5 - 2.25 * ((exit_price - k_short) / strike_step))
            else: # BEARISH
                k_short = k_long - strike_step
                if exit_price >= k_long:
                    return 0.25
                elif exit_price >= k_short:
                    return 0.25 + 2.25 * ((k_long - exit_price) / strike_step)
                else:
                    return max(-1.0, 2.5 - 2.25 * ((k_short - exit_price) / strike_step))

    elif strategy_type == "Credit":
        # Credit Spread Model
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
                return -1.0 + (1.0 + max_profit) * ((exit_price - original_stop) / (entry_price - original_stop))
            elif exit_reason in ("TIME_EXIT", "NO_EXIT_BARS"):
                if exit_price >= entry_price:
                    return max_profit
                if exit_price <= original_stop:
                    return -1.0
                return -1.0 + (1.0 + max_profit) * ((exit_price - original_stop) / (entry_price - original_stop))
            else:
                if exit_price >= entry_price:
                    return max_profit
                if exit_price <= original_stop:
                    return -1.0
                return -1.0 + (1.0 + max_profit) * ((exit_price - original_stop) / (entry_price - original_stop))
        else: # BEARISH
            if exit_reason == "STOP":
                if exit_price >= original_stop:
                    return -1.0
                # Trailed stop
                if exit_price <= entry_price:
                    return max_profit
                return -1.0 + (1.0 + max_profit) * ((original_stop - exit_price) / (original_stop - entry_price))
            elif exit_reason in ("TIME_EXIT", "NO_EXIT_BARS"):
                if exit_price <= entry_price:
                    return max_profit
                if exit_price >= original_stop:
                    return -1.0
                return -1.0 + (1.0 + max_profit) * ((original_stop - exit_price) / (original_stop - entry_price))
            else:
                if exit_price <= entry_price:
                    return max_profit
                if exit_price >= original_stop:
                    return -1.0
                return -1.0 + (1.0 + max_profit) * ((original_stop - exit_price) / (original_stop - entry_price))

    else: # Debit Spread Model
        if exit_reason == "TARGET":
            return 1.75

        if direction == "BULLISH":
            r_linear = (exit_price - entry_price) / risk
        else:
            r_linear = (entry_price - exit_price) / risk

        return min(1.75, max(-1.0, r_linear))


def backtest_symbol(symbol: str, candles: pd.DataFrame, params: BacktestParams) -> list[Trade]:
    trades = []
    if candles.empty:
        return trades

    dates = sorted(candles["date"].unique())
    if len(dates) < 2:
        return trades

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

            bull_bos = close > swing_high and close > bull_level and close > float(row["vwap"])
            if bull_swept and bull_bos:
                # Calculate impulse leg size from close to the extreme point of liquidity sweep
                sweep_low_val = float(sweep_low or low)
                impulse_leg_size = close - sweep_low_val
                atr_14 = float(row.get("atr14") or 0.0)
                if math.isnan(atr_14):
                    atr_14 = 0.0

                # Adaptive Strategy Selection Engine: Route to Credit/Ratio Spreads for high-probability decay
                strategy_type = None
                regime = ""
                if impulse_leg_size >= atr_14 * 1.5:
                    regime = "Mean-Reverting Grind"
                    strategy_type = params.strategy_type

                if strategy_type is not None:
                    score, notes = score_setup(row, "BULLISH", True, True, volume_ok, atr_ok, params)
                    
                    # Mid-Day Volume Lull Parameter check
                    required_score = params.min_score
                    time_str = str(row["time"])
                    if "11:30" <= time_str <= "13:30":
                        required_score += 5
                        notes = notes + ["mid_day_lull_raised_threshold"]
                        
                    if score >= required_score:
                        # Anchor stop-loss to the sweep invalidation level to prevent micro-stopouts
                        stop = min(float(sweep_low or low), bull_level) - sweep_buffer
                        risk = close - stop
                        if risk > 0:
                            # Calculate target based on strategy type
                            if strategy_type == "Debit":
                                target = close + (risk * 1.75)  # Open target of +1.75R
                            elif strategy_type == "Ratio":
                                strike_step = get_strike_step(symbol)
                                target = close + strike_step
                            else: # Credit
                                # Estimate the full daily range by scaling the 5-minute ATR up to a daily boundary proxy
                                daily_atr_estimate = atr_14 * 15  
                                # Establish the daily extension boundaries based on the opening print of the current day
                                day_open = float(day.iloc[0]["open"])
                                expected_daily_high = day_open + daily_atr_estimate
                                # Calculate our standard target based on risk reward
                                standard_target = close + (risk * params.rr_target)
                                # Cap the target so it does not exceed the statistically expected daily extension boundary
                                if atr_14 > 0:
                                    target = min(standard_target, expected_daily_high)
                                else:
                                    target = standard_target
                                    
                            original_stop = stop
                            exit_price, exit_dt, exit_reason, bars_elapsed, total_bars = simulate_exit(day, idx, "BULLISH", close, stop, target, params)
                            
                            strike_step = get_strike_step(symbol)
                            rr_realized = calculate_realized_rr(
                                strategy_type=strategy_type,
                                direction="BULLISH",
                                entry_price=close,
                                original_stop=original_stop,
                                exit_price=exit_price,
                                exit_reason=exit_reason,
                                credit_premium_pct=params.credit_premium_pct,
                                strike_step=strike_step
                            )
                            
                            # Discrete lot sizing logic check
                            if params.discrete_risk_budget is not None and params.discrete_risk_budget > 0:
                                lot_size = 65 if symbol == "NIFTY" else (20 if symbol == "SENSEX" else 1)
                                if strategy_type == "Ratio":
                                    margin_per_lot = get_margin_per_lot(symbol, close)
                                    risk_budget_rupees = params.capital * 0.005 # 0.5% portfolio risk budget
                                    risk_per_lot = 0.20 * strike_step * lot_size
                                    lots_by_risk = math.floor(risk_budget_rupees / risk_per_lot)
                                    lots_by_margin = math.floor(params.capital / margin_per_lot)
                                    lots = min(lots_by_risk, lots_by_margin)
                                    if lots < 1:
                                        continue
                                    risk_amount = lots * risk_per_lot
                                    pnl_points = rr_realized * (0.20 * strike_step)
                                    notes_with_lots = notes + [f"lots={lots}", f"strike_step={strike_step}", f"margin={lots * margin_per_lot}"]
                                else: # Credit
                                    spread_width = params.nifty_spread_width if symbol == "NIFTY" else (params.sensex_spread_width if symbol == "SENSEX" else params.reliance_spread_width)
                                    net_risk_per_share = (1.0 - params.credit_premium_pct) * spread_width
                                    risk_per_lot = net_risk_per_share * lot_size
                                    lots = math.floor(params.discrete_risk_budget / risk_per_lot)
                                    if lots < 1:
                                        continue # Skip this trade due to lot size constraint
                                    risk_amount = lots * risk_per_lot
                                    pnl_points = rr_realized * net_risk_per_share
                                    notes_with_lots = notes + [f"lots={lots}", f"spread_width={spread_width}"]
                            else:
                                pnl_points = rr_realized * risk
                                risk_amount = params.capital * params.risk_per_trade_pct
                                notes_with_lots = notes
                                
                            pnl_amount = risk_amount * rr_realized
                            spread_tag = "bull_call_debit_spread" if strategy_type == "Debit" else ("bull_ratio_spread" if strategy_type == "Ratio" else "bull_put_credit_spread")
                            notes_with_spread = notes_with_lots + [spread_tag, strategy_type, regime]
                            
                            trades.append(Trade(
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
                                rr_realized=round(rr_realized, 4),
                                risk_amount=round(risk_amount, 2),
                                pnl_amount=round(pnl_amount, 2),
                                score=score,
                                exit_reason=exit_reason,
                                setup_notes=";".join(notes_with_spread),
                            ))
                            traded_today = True
                            continue

            bear_bos = close < swing_low and close < bear_level and close < float(row["vwap"])
            if bear_swept and bear_bos:
                # Calculate impulse leg size from extreme point of sweep to close
                sweep_high_val = float(sweep_high or high)
                impulse_leg_size = sweep_high_val - close
                atr_14 = float(row.get("atr14") or 0.0)
                if math.isnan(atr_14):
                    atr_14 = 0.0

                # Adaptive Strategy Selection Engine: Route to Credit/Ratio Spreads for high-probability decay
                strategy_type = None
                regime = ""
                if impulse_leg_size >= atr_14 * 1.5:
                    regime = "Mean-Reverting Grind"
                    strategy_type = params.strategy_type

                if strategy_type is not None:
                    score, notes = score_setup(row, "BEARISH", True, True, volume_ok, atr_ok, params)
                    
                    # Mid-Day Volume Lull Parameter check
                    required_score = params.min_score
                    time_str = str(row["time"])
                    if "11:30" <= time_str <= "13:30":
                        required_score += 5
                        notes = notes + ["mid_day_lull_raised_threshold"]
                        
                    if score >= required_score:
                        # Anchor stop-loss to the sweep invalidation level to prevent micro-stopouts
                        stop = max(float(sweep_high or high), bear_level) + sweep_buffer
                        risk = stop - close
                        if risk > 0:
                            # Calculate target based on strategy type
                            if strategy_type == "Debit":
                                target = close - (risk * 1.75)  # Open target of +1.75R
                            elif strategy_type == "Ratio":
                                strike_step = get_strike_step(symbol)
                                target = close - strike_step
                            else: # Credit
                                # Estimate the full daily range by scaling the 5-minute ATR up to a daily boundary proxy
                                daily_atr_estimate = atr_14 * 15  
                                # Establish the daily extension boundaries based on the opening print of the current day
                                day_open = float(day.iloc[0]["open"])
                                expected_daily_high = day_open - daily_atr_estimate
                                # Calculate our standard target based on risk reward
                                standard_target = close - (risk * params.rr_target)
                                # Cap the target so it does not drop below the statistically expected daily extension boundary
                                if atr_14 > 0:
                                    target = max(standard_target, expected_daily_high)
                                else:
                                    target = standard_target
                                    
                            original_stop = stop
                            exit_price, exit_dt, exit_reason, bars_elapsed, total_bars = simulate_exit(day, idx, "BEARISH", close, stop, target, params)
                            
                            strike_step = get_strike_step(symbol)
                            rr_realized = calculate_realized_rr(
                                strategy_type=strategy_type,
                                direction="BEARISH",
                                entry_price=close,
                                original_stop=original_stop,
                                exit_price=exit_price,
                                exit_reason=exit_reason,
                                credit_premium_pct=params.credit_premium_pct,
                                strike_step=strike_step
                            )
                            
                            # Discrete lot sizing logic check
                            if params.discrete_risk_budget is not None and params.discrete_risk_budget > 0:
                                lot_size = 65 if symbol == "NIFTY" else (20 if symbol == "SENSEX" else 1)
                                if strategy_type == "Ratio":
                                    margin_per_lot = get_margin_per_lot(symbol, close)
                                    risk_budget_rupees = params.capital * 0.005 # 0.5% portfolio risk budget
                                    risk_per_lot = 0.20 * strike_step * lot_size
                                    lots_by_risk = math.floor(risk_budget_rupees / risk_per_lot)
                                    lots_by_margin = math.floor(params.capital / margin_per_lot)
                                    lots = min(lots_by_risk, lots_by_margin)
                                    if lots < 1:
                                        continue
                                    risk_amount = lots * risk_per_lot
                                    pnl_points = rr_realized * (0.20 * strike_step)
                                    notes_with_lots = notes + [f"lots={lots}", f"strike_step={strike_step}", f"margin={lots * margin_per_lot}"]
                                else: # Credit
                                    spread_width = params.nifty_spread_width if symbol == "NIFTY" else (params.sensex_spread_width if symbol == "SENSEX" else params.reliance_spread_width)
                                    net_risk_per_share = (1.0 - params.credit_premium_pct) * spread_width
                                    risk_per_lot = net_risk_per_share * lot_size
                                    lots = math.floor(params.discrete_risk_budget / risk_per_lot)
                                    if lots < 1:
                                        continue # Skip this trade due to lot size constraint
                                    risk_amount = lots * risk_per_lot
                                    pnl_points = rr_realized * net_risk_per_share
                                    notes_with_lots = notes + [f"lots={lots}", f"spread_width={spread_width}"]
                            else:
                                pnl_points = rr_realized * risk
                                risk_amount = params.capital * params.risk_per_trade_pct
                                notes_with_lots = notes
                                
                            pnl_amount = risk_amount * rr_realized
                            spread_tag = "bear_put_debit_spread" if strategy_type == "Debit" else ("bear_ratio_spread" if strategy_type == "Ratio" else "bear_call_credit_spread")
                            notes_with_spread = notes_with_lots + [spread_tag, strategy_type, regime]
                            
                            trades.append(Trade(
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
                                rr_realized=round(rr_realized, 4),
                                risk_amount=round(risk_amount, 2),
                                pnl_amount=round(pnl_amount, 2),
                                score=score,
                                exit_reason=exit_reason,
                                setup_notes=";".join(notes_with_spread),
                            ))
                            traded_today = True

        previous_levels[date] = prior_day_levels(day)
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
        logging.warning("Permission denied writing to %s. Trying backup path in backtests_temp...", path)
        backup_path = path.parent.parent / "backtests_temp" / path.name
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        with backup_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        logging.info("Successfully wrote backup file to %s", backup_path)


def params_from_args(args: argparse.Namespace, min_score=None, rr_target=None) -> BacktestParams:
    return BacktestParams(
        min_score=int(args.min_score if min_score is None else min_score),
        rr_target=float(args.rr_target if rr_target is None else rr_target),
        risk_per_trade_pct=float(args.risk_per_trade_pct),
        capital=float(args.capital),
        oi_mode=str(args.oi_mode),
        swing_lookback=int(args.swing_lookback),
        credit_premium_pct=float(args.credit_premium_pct if hasattr(args, "credit_premium_pct") else 0.9090909090909091),
        discrete_risk_budget=float(args.discrete_risk_budget) if getattr(args, "discrete_risk_budget", None) is not None else None,
        nifty_spread_width=float(args.nifty_spread_width) if getattr(args, "nifty_spread_width", None) is not None else 10.0,
        sensex_spread_width=float(args.sensex_spread_width) if getattr(args, "sensex_spread_width", None) is not None else 20.0,
        reliance_spread_width=float(args.reliance_spread_width) if getattr(args, "reliance_spread_width", None) is not None else 2.0,
        strategy_type=str(getattr(args, "strategy", "Credit"))
    )


def run_for_params(selected: list[dict], args: argparse.Namespace, params: BacktestParams):
    input_dir = Path(args.input_dir)
    all_trades = []
    summary_rows = []

    for position, instrument in enumerate(selected, start=1):
        symbol = instrument["symbol"]
        path = input_dir / f"{symbol}_{args.interval}m.csv"
        logging.info("[%s/%s] Backtesting %s from %s", position, len(selected), symbol, path)
        if not path.exists():
            logging.warning("[%s] missing data file: %s", symbol, path)
            summary_rows.append(summarize(symbol, []))
            continue
        candles = load_candles(path)
        trades = backtest_symbol(symbol, candles, params)
        all_trades.extend(trades)
        row = summarize(symbol, trades)
        summary_rows.append(row)
        logging.info(
            "[%s] trades=%s win_rate=%.2f%% expectancy=%.2fR total=%.2fR",
            symbol,
            row["trades"],
            row["win_rate"] * 100,
            row["expectancy_rr"],
            row["total_rr"],
        )
    return all_trades, summary_rows


def optimization_rows(selected: list[dict], args: argparse.Namespace) -> list[dict]:
    rows = []
    for min_score, rr_target in itertools.product([70, 75, 80, 85], [1.5, 1.75, 2.0]):
        params = params_from_args(args, min_score=min_score, rr_target=rr_target)
        trades, summary = run_for_params(selected, args, params)
        total_trades = sum(row["trades"] for row in summary)
        total_rr = sum(row["total_rr"] for row in summary)
        expectancy = total_rr / total_trades if total_trades else 0.0
        win_count = len([t for t in trades if t.rr_realized > 0])
        rows.append({
            "min_score": min_score,
            "rr_target": rr_target,
            "symbols": len(selected),
            "trades": total_trades,
            "win_rate": round(win_count / total_trades, 4) if total_trades else 0.0,
            "expectancy_rr": round(expectancy, 4),
            "total_rr": round(total_rr, 4),
            "max_drawdown_rr": round(max_drawdown([float(t.rr_realized) for t in trades]), 4),
        })
        logging.info(
            "[grid] score=%s rr=%.2f trades=%s expectancy=%.2fR total=%.2fR",
            min_score,
            rr_target,
            total_trades,
            expectancy,
            total_rr,
        )
    return rows


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    setup_logging(output_dir)
    instruments = select_universe(args.symbols, args.universe_file)
    selected, total_batches = batch_universe(instruments, batch=args.batch, batch_size=args.batch_size)
    logging.info(
        "Selected batch %s/%s with %s instruments: %s",
        args.batch,
        total_batches,
        len(selected),
        ", ".join(item["symbol"] for item in selected),
    )

    if args.optimize:
        rows = optimization_rows(selected, args)
        write_csv(
            output_dir / "optimization_grid.csv",
            rows,
            ["min_score", "rr_target", "symbols", "trades", "win_rate", "expectancy_rr", "total_rr", "max_drawdown_rr"],
        )
        logging.info("Optimization grid written to %s", output_dir / "optimization_grid.csv")

    params = params_from_args(args)
    trades, summary_rows = run_for_params(selected, args, params)
    trade_rows = [asdict(trade) for trade in trades]
    write_csv(
        output_dir / "backtest_trades.csv",
        trade_rows,
        TRADE_FIELDS,
    )
    write_csv(
        output_dir / "backtest_summary.csv",
        summary_rows,
        ["symbol", "trades", "win_rate", "avg_rr", "expectancy_rr", "total_rr", "profit_factor", "max_drawdown_rr", "best_rr", "worst_rr"],
    )
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
    raise SystemExit(main())
