"""Paper-first intraday liquidity-sweep credit spread strategy."""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd

import config
import state_manager
from logger import log_trade

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDIT_SWEEP_STATE_FILE = os.path.join(BASE_DIR, "credit_sweep_state.json")

STRATEGY_BULL_PUT = "CREDIT_SWEEP_BULL_PUT"
STRATEGY_BEAR_CALL = "CREDIT_SWEEP_BEAR_CALL"
STRATEGY_PAPER = "CREDIT_SWEEP_PAPER"


@dataclass
class CreditSweepSignal:
    symbol: str
    direction: str = ""
    status: str = "NO_SIGNAL"
    reject_reason: str = ""
    score: int = 0
    entry_price: float = 0.0
    stop_price: float = 0.0
    target_price: float = 0.0
    risk_points: float = 0.0
    reward_points: float = 0.0
    rr_target: float = 0.0
    signal_time: str = ""
    signal_dt: str = ""
    signal_age_seconds: int = 0
    vwap: float = 0.0
    atr14: float = 0.0
    volume: float = 0.0
    volume_median20: float = 0.0
    bull_level: float = 0.0
    bear_level: float = 0.0
    sweep_extreme: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def confirmed(self) -> bool:
        return self.status == "CONFIRMED"

    def to_row(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        row = asdict(self)
        row["confirmed"] = self.confirmed
        if extra:
            row.update(extra)
        return row


def load_credit_sweep_state(path: str = CREDIT_SWEEP_STATE_FILE) -> dict[str, Any]:
    if not os.path.exists(path):
        return {"active": False, "scanner": [], "paper_trades": []}
    try:
        with open(path, "r", encoding="utf-8") as file:
            payload = json.load(file)
            if isinstance(payload, dict):
                payload.setdefault("active", False)
                payload.setdefault("scanner", [])
                payload.setdefault("paper_trades", [])
                return payload
    except Exception:
        logging.exception("Failed to load Credit Sweep state.")
    return {"active": False, "scanner": [], "paper_trades": []}


def save_credit_sweep_state(payload: dict[str, Any], path: str = CREDIT_SWEEP_STATE_FILE) -> None:
    payload["updated_at"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state_manager._atomic_write(path, payload)


def parse_hhmm(value: str, default: str = "09:30") -> dt.time:
    try:
        return dt.datetime.strptime(str(value), "%H:%M").time()
    except ValueError:
        return dt.datetime.strptime(default, "%H:%M").time()


def normalize_candles(candles: list | pd.DataFrame) -> pd.DataFrame:
    if isinstance(candles, pd.DataFrame):
        df = candles.copy()
    else:
        df = pd.DataFrame(candles, columns=["datetime", "open", "high", "low", "close", "volume", "oi"])

    if df.empty:
        return df

    if "dt" not in df.columns:
        raw_dt = df["datetime"]
        if pd.api.types.is_numeric_dtype(raw_dt):
            df["dt"] = pd.to_datetime(raw_dt, unit="s", errors="coerce")
        else:
            df["dt"] = pd.to_datetime(raw_dt, errors="coerce")
    else:
        df["dt"] = pd.to_datetime(df["dt"], errors="coerce")

    df = df.dropna(subset=["dt"]).sort_values("dt").copy()
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"])
    df["volume"] = df["volume"].fillna(0.0)
    df["date"] = df["dt"].dt.date.astype(str)
    df["time"] = df["dt"].dt.strftime("%H:%M")

    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    df["pv"] = typical * df["volume"].clip(lower=1.0)
    df["day_cum_pv"] = df.groupby("date")["pv"].cumsum()
    df["day_cum_volume"] = df.groupby("date")["volume"].transform(lambda s: s.clip(lower=1.0).cumsum())
    df["vwap"] = df["day_cum_pv"] / df["day_cum_volume"]

    prev_close = df["close"].shift(1)
    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    df["atr14"] = true_range.rolling(14, min_periods=5).mean().fillna(df["high"] - df["low"])
    df["volume_median20"] = df["volume"].rolling(20, min_periods=5).median().fillna(0.0)
    return df


def prior_day_levels(candles: pd.DataFrame, current_date: str) -> dict[str, float] | None:
    if candles.empty or "date" not in candles.columns:
        return None
    history = candles[candles["date"] < str(current_date)]
    if history.empty:
        return None
    daily = history.groupby("date").agg(pdh=("high", "max"), pdl=("low", "min"), pdc=("close", "last"))
    latest_date = sorted(daily.index)[-1]
    row = daily.loc[latest_date]
    return {"pdh": float(row["pdh"]), "pdl": float(row["pdl"]), "pdc": float(row["pdc"])}


def _last_closed_index(day: pd.DataFrame, now: dt.datetime | None, interval_minutes: int) -> tuple[int | None, int]:
    if day.empty:
        return None, 0
    idx = len(day) - 1
    if now is None:
        return idx, 0

    while idx >= 0:
        candle_open = pd.Timestamp(day.iloc[idx]["dt"]).to_pydatetime()
        candle_close = candle_open + dt.timedelta(minutes=interval_minutes)
        if now >= candle_close:
            age = max(0, int((now - candle_close).total_seconds()))
            return idx, age
        idx -= 1
    return None, 0


def _score_signal(row: pd.Series, direction: str, volume_ok: bool, atr_ok: bool) -> tuple[int, list[str]]:
    close = float(row["close"])
    vwap = float(row["vwap"])
    open_price = float(row["open"])
    notes = ["BOS", "liquidity_sweep"]
    score = 40

    if (direction == "BULLISH" and close > vwap) or (direction == "BEARISH" and close < vwap):
        score += 10
        notes.append("vwap_aligned")
    if volume_ok:
        score += 20
        notes.extend(["oi_proxy_volume", "volume_expansion"])
    else:
        score += 10
        notes.append("oi_proxy_partial")
    if atr_ok:
        score += 10
        notes.append("greeks_iv_proxy_ok")
    premium_ok = (
        (direction == "BULLISH" and close >= open_price)
        or (direction == "BEARISH" and close <= open_price)
    )
    if premium_ok:
        score += 10
        notes.append("premium_momentum_proxy")
    return min(score, 100), notes


def _target_for_signal(day: pd.DataFrame, row: pd.Series, direction: str, entry: float, stop: float) -> float:
    risk = entry - stop if direction == "BULLISH" else stop - entry
    standard_target = entry + risk * config.CREDIT_SWEEP_RR_TARGET if direction == "BULLISH" else entry - risk * config.CREDIT_SWEEP_RR_TARGET
    atr = float(row.get("atr14") or 0.0)
    if atr <= 0 or day.empty:
        return standard_target
    day_open = float(day.iloc[0]["open"])
    daily_atr_estimate = atr * 15.0
    if direction == "BULLISH":
        return min(standard_target, day_open + daily_atr_estimate)
    return max(standard_target, day_open - daily_atr_estimate)


def _reject(symbol: str, reason: str, status: str = "REJECTED") -> CreditSweepSignal:
    return CreditSweepSignal(symbol=symbol, status=status, reject_reason=reason)


def evaluate_credit_sweep_signal(
    symbol: str,
    candles: pd.DataFrame,
    levels: dict[str, float] | None,
    now: dt.datetime | None = None,
    interval_minutes: int = 5,
) -> CreditSweepSignal:
    symbol = str(symbol).upper().strip()
    if symbol not in {str(item).upper().strip() for item in config.CREDIT_SWEEP_SYMBOLS}:
        return _reject(symbol, "Symbol not enabled for Credit Sweep")

    if candles.empty:
        return _reject(symbol, "No candles available", status="NO_SIGNAL")
    if not levels:
        return _reject(symbol, "Previous day levels unavailable", status="NO_SIGNAL")

    day = candles.sort_values("dt").reset_index(drop=True).copy()
    last_idx, signal_age = _last_closed_index(day, now, interval_minutes)
    if last_idx is None:
        return _reject(symbol, "No fully closed candle", status="NO_SIGNAL")

    row = day.iloc[last_idx]
    time_str = str(row["time"])
    if time_str < str(config.CREDIT_SWEEP_ENTRY_START) or time_str > str(config.CREDIT_SWEEP_ENTRY_CUTOFF):
        return _reject(symbol, f"Time {time_str} outside Credit Sweep window")
    if signal_age > int(config.CREDIT_SWEEP_MAX_SIGNAL_AGE_SECONDS):
        return _reject(symbol, f"Signal stale: {signal_age}s old")

    entry_start = str(config.CREDIT_SWEEP_ENTRY_START)
    opening = day[day["time"] < entry_start]
    if opening.empty:
        return _reject(symbol, "Opening range unavailable", status="NO_SIGNAL")

    orh = float(opening["high"].max())
    orl = float(opening["low"].min())
    bull_level = min(float(levels["pdl"]), orl)
    bear_level = max(float(levels["pdh"]), orh)

    scan = day[(day["time"] >= entry_start) & (day.index <= last_idx)]
    bull_swept = False
    bear_swept = False
    sweep_low = None
    sweep_high = None
    sweep_buffer = 0.0

    for idx, scan_row in scan.iterrows():
        close = float(scan_row["close"])
        high = float(scan_row["high"])
        low = float(scan_row["low"])
        sweep_buffer = close * float(config.CREDIT_SWEEP_SWEEP_BUFFER_PCT)

        if low < bull_level - sweep_buffer:
            bull_swept = True
            sweep_low = low if sweep_low is None else min(sweep_low, low)
        if high > bear_level + sweep_buffer:
            bear_swept = True
            sweep_high = high if sweep_high is None else max(sweep_high, high)

        if idx != last_idx:
            continue

        if idx < int(config.CREDIT_SWEEP_SWING_LOOKBACK) + 1:
            return _reject(symbol, "Swing window unavailable", status="NO_SIGNAL")

        previous_window = day.iloc[max(0, idx - int(config.CREDIT_SWEEP_SWING_LOOKBACK)):idx]
        if previous_window.empty:
            return _reject(symbol, "Swing window unavailable", status="NO_SIGNAL")

        swing_high = float(previous_window["high"].max())
        swing_low = float(previous_window["low"].min())
        volume_base = float(scan_row.get("volume_median20") or 0.0)
        volume_ok = volume_base <= 0 or float(scan_row["volume"]) >= volume_base * float(config.CREDIT_SWEEP_VOLUME_MULTIPLIER)
        atr = float(scan_row.get("atr14") or 0.0)
        atr_pct = atr / close if close > 0 else 0.0
        atr_ok = 0.0005 <= atr_pct <= float(config.CREDIT_SWEEP_MAX_ATR_PCT)

        bull_bos = bull_swept and close > swing_high and close > bull_level and close > float(scan_row["vwap"])
        bear_bos = bear_swept and close < swing_low and close < bear_level and close < float(scan_row["vwap"])
        direction = "BULLISH" if bull_bos else "BEARISH" if bear_bos else ""
        if not direction:
            return _reject(symbol, "Sweep present but no latest BOS/VWAP confirmation", status="NO_SIGNAL")

        impulse = (close - float(sweep_low or low)) if direction == "BULLISH" else (float(sweep_high or high) - close)
        if atr > 0 and impulse < atr * 1.5:
            return _reject(symbol, "Impulse below 1.5 ATR displacement")

        score, notes = _score_signal(scan_row, direction, volume_ok, atr_ok)
        required_score = int(config.CREDIT_SWEEP_MIN_SCORE)
        if "11:30" <= time_str <= "13:30":
            required_score += 5
            notes.append("mid_day_lull_raised_threshold")
        if score < required_score:
            return _reject(symbol, f"Score {score} below required {required_score}")

        if direction == "BULLISH":
            stop = min(float(sweep_low or low), bull_level) - sweep_buffer
            risk = close - stop
            sweep_extreme = float(sweep_low or low)
        else:
            stop = max(float(sweep_high or high), bear_level) + sweep_buffer
            risk = stop - close
            sweep_extreme = float(sweep_high or high)
        if risk <= 0:
            return _reject(symbol, "Invalid non-positive risk")

        target = _target_for_signal(day, scan_row, direction, close, stop)
        reward = abs(target - close)
        if reward <= 0:
            return _reject(symbol, "Invalid non-positive reward")

        return CreditSweepSignal(
            symbol=symbol,
            direction=direction,
            status="CONFIRMED",
            score=score,
            entry_price=round(close, 4),
            stop_price=round(stop, 4),
            target_price=round(target, 4),
            risk_points=round(risk, 4),
            reward_points=round(reward, 4),
            rr_target=round(reward / risk, 4),
            signal_time=time_str,
            signal_dt=str(scan_row["dt"]),
            signal_age_seconds=signal_age,
            vwap=round(float(scan_row["vwap"]), 4),
            atr14=round(atr, 4),
            volume=round(float(scan_row["volume"]), 4),
            volume_median20=round(volume_base, 4),
            bull_level=round(bull_level, 4),
            bear_level=round(bear_level, 4),
            sweep_extreme=round(sweep_extreme, 4),
            notes=notes,
        )

    return _reject(symbol, "Not enough candles after entry start", status="NO_SIGNAL")


def net_credit(entry_prices: dict[str, float], order_sequence: list[tuple[str, str]]) -> float:
    total = 0.0
    for leg_name, transaction_type in order_sequence or []:
        price = float(entry_prices.get(leg_name, 0.0) or 0.0)
        total += price if str(transaction_type).upper() == "SELL" else -price
    return total


def spread_width(strikes: dict[str, float]) -> float:
    values = [float(value) for key, value in (strikes or {}).items() if key != "atm" and value not in (None, "")]
    return max(values) - min(values) if len(values) >= 2 else 0.0


def defined_loss(entry_prices: dict[str, float], strikes: dict[str, float], order_sequence: list[tuple[str, str]], quantity: int = 1) -> float:
    credit = net_credit(entry_prices, order_sequence)
    width = spread_width(strikes)
    if credit <= 0 or width <= 0:
        return 0.0
    return max(0.0, (width - credit) * int(quantity or 1))


def validate_credit_spread_route(route: Any, risk_budget: float | None = None, quantity: int | None = None) -> tuple[bool, str, dict[str, float]]:
    risk_budget = float(config.CREDIT_SWEEP_RISK_BUDGET if risk_budget is None else risk_budget)
    quantity = int(config.CREDIT_SWEEP_PAPER_QUANTITY if quantity is None else quantity)
    if not route or getattr(route, "no_trade_reason", ""):
        return False, getattr(route, "no_trade_reason", "Route unavailable"), {}
    entry_prices = getattr(route, "entry_prices", {}) or {}
    order_sequence = getattr(route, "order_sequence", []) or []
    strikes = getattr(route, "strikes", {}) or {}
    credit = net_credit(entry_prices, order_sequence)
    width = spread_width(strikes)
    loss = defined_loss(entry_prices, strikes, order_sequence, quantity=quantity)
    metrics = {"net_credit": round(credit, 4), "spread_width": round(width, 4), "defined_loss": round(loss, 4)}
    if credit <= 0:
        return False, "Option spread is not a real credit", metrics
    if loss <= 0:
        return False, "Defined loss is not positive", metrics
    if risk_budget > 0 and loss > risk_budget:
        return False, f"Defined loss {loss:.2f} exceeds risk budget {risk_budget:.2f}", metrics
    return True, "OK", metrics


def validate_live_price_distance(signal: CreditSweepSignal, fresh_spot: float) -> tuple[bool, str]:
    if not signal.confirmed:
        return False, signal.reject_reason or "Signal not confirmed"
    fresh_spot = float(fresh_spot or 0.0)
    if fresh_spot <= 0:
        return False, "Fresh spot unavailable"

    reward = abs(signal.target_price - signal.entry_price)
    risk = abs(signal.entry_price - signal.stop_price)
    min_target_remaining = max(reward * float(config.CREDIT_SWEEP_TARGET_NEAR_FRACTION), signal.atr14 * 0.20)
    min_stop_distance = max(risk * float(config.CREDIT_SWEEP_STOP_NEAR_FRACTION), signal.atr14 * 0.20)

    if signal.direction == "BULLISH":
        target_remaining = signal.target_price - fresh_spot
        stop_distance = fresh_spot - signal.stop_price
    else:
        target_remaining = fresh_spot - signal.target_price
        stop_distance = signal.stop_price - fresh_spot

    if target_remaining <= min_target_remaining:
        return False, "Current price is already too close to target"
    if stop_distance <= min_stop_distance:
        return False, "Current price is already too close to stop"
    return True, "OK"


def strategy_type_for_direction(direction: str) -> str:
    return STRATEGY_BULL_PUT if direction == "BULLISH" else STRATEGY_BEAR_CALL


def _paper_rr_from_spot(position: dict[str, Any], spot_price: float) -> float:
    direction = position.get("direction")
    entry = float(position.get("entry_spot", 0.0) or 0.0)
    stop = float(position.get("stop_price", 0.0) or 0.0)
    target = float(position.get("target_price", 0.0) or 0.0)
    net = float(position.get("net_credit", 0.0) or 0.0)
    loss = float(position.get("defined_loss", 0.0) or 0.0)
    max_profit_r = net / loss if loss > 0 else 0.0
    if entry <= 0 or stop <= 0 or target <= 0:
        return 0.0
    if direction == "BULLISH":
        if spot_price <= stop:
            return -1.0
        if spot_price >= target:
            return max_profit_r
        if spot_price >= entry:
            return max_profit_r * ((spot_price - entry) / max(0.01, target - entry))
        return -1.0 + ((spot_price - stop) / max(0.01, entry - stop))
    if spot_price >= stop:
        return -1.0
    if spot_price <= target:
        return max_profit_r
    if spot_price <= entry:
        return max_profit_r * ((entry - spot_price) / max(0.01, entry - target))
    return -1.0 + ((stop - spot_price) / max(0.01, stop - entry))


def paper_mark_to_market(position: dict[str, Any], spot_price: float, current_prices: dict[str, float] | None = None) -> dict[str, Any]:
    qty = int(position.get("quantity", config.CREDIT_SWEEP_PAPER_QUANTITY) or 1)
    entry_net = float(position.get("net_credit", 0.0) or 0.0)
    risk_budget = float(position.get("risk_budget", config.CREDIT_SWEEP_RISK_BUDGET) or 0.0)
    if current_prices:
        live_net = net_credit(current_prices, position.get("order_sequence", []))
        pnl = (entry_net - live_net) * qty
        rr = pnl / risk_budget if risk_budget > 0 else 0.0
    else:
        rr = _paper_rr_from_spot(position, float(spot_price or 0.0))
        pnl = rr * risk_budget
        live_net = 0.0
    return {
        "current_spot": round(float(spot_price or 0.0), 4),
        "live_net_credit": round(live_net, 4),
        "paper_pnl": round(pnl, 2),
        "paper_rr": round(rr, 4),
    }


def paper_exit_reason(position: dict[str, Any], spot_price: float, now: dt.datetime | None = None) -> str | None:
    now = now or dt.datetime.now()
    direction = position.get("direction")
    stop = float(position.get("stop_price", 0.0) or 0.0)
    target = float(position.get("target_price", 0.0) or 0.0)
    if direction == "BULLISH":
        if spot_price <= stop:
            return "STOP"
        if spot_price >= target:
            return "TARGET"
    elif direction == "BEARISH":
        if spot_price >= stop:
            return "STOP"
        if spot_price <= target:
            return "TARGET"
    if now.time() >= parse_hhmm(config.CREDIT_SWEEP_EXIT_TIME, "15:00"):
        return "TIME_EXIT"
    return None


def record_paper_entry(signal: CreditSweepSignal, route: Any, metrics: dict[str, float], fresh_spot: float) -> dict[str, Any]:
    quantity = int(config.CREDIT_SWEEP_PAPER_QUANTITY)
    position = {
        "active": True,
        "paper_only": True,
        "symbol": signal.symbol,
        "strategy_type": strategy_type_for_direction(signal.direction),
        "direction": signal.direction,
        "created_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "entry_spot": round(float(fresh_spot), 4),
        "signal_entry_price": signal.entry_price,
        "stop_price": signal.stop_price,
        "target_price": signal.target_price,
        "risk_points": signal.risk_points,
        "reward_points": signal.reward_points,
        "score": signal.score,
        "signal_time": signal.signal_time,
        "signal_dt": signal.signal_dt,
        "legs": getattr(route, "legs", {}) or {},
        "entry_prices": getattr(route, "entry_prices", {}) or {},
        "strikes": getattr(route, "strikes", {}) or {},
        "order_sequence": getattr(route, "order_sequence", []) or [],
        "net_credit": metrics.get("net_credit", 0.0),
        "spread_width": metrics.get("spread_width", 0.0),
        "defined_loss": metrics.get("defined_loss", 0.0),
        "quantity": quantity,
        "risk_budget": float(config.CREDIT_SWEEP_RISK_BUDGET),
        "notes": ";".join(signal.notes + ["paper_only"]),
    }
    log_trade(
        "ENTRY",
        signal.symbol,
        position["entry_prices"],
        position["net_credit"],
        0.0,
        "Credit Sweep Paper Trade (Simulated)",
        spot_price=fresh_spot,
        strikes=position["strikes"],
        strategy_type=STRATEGY_PAPER,
        broker_lot_size=1,
        total_lots_deployed=1,
        total_quantity=quantity,
        margin_blocked=position["defined_loss"],
    )
    return position


def record_paper_exit(position: dict[str, Any], mark: dict[str, Any], exit_reason: str) -> dict[str, Any]:
    exit_prices = position.get("entry_prices", {})
    if mark.get("live_net_credit", 0.0) > 0:
        exit_prices = position.get("last_live_prices", exit_prices)
    log_trade(
        "EXIT",
        position.get("symbol", ""),
        exit_prices,
        mark.get("live_net_credit", position.get("net_credit", 0.0)),
        mark.get("paper_pnl", 0.0),
        "Credit Sweep Paper Trade Closed",
        spot_price=mark.get("current_spot", 0.0),
        strikes=position.get("strikes", {}),
        exit_reason=exit_reason,
        strategy_type=STRATEGY_PAPER,
        broker_lot_size=1,
        total_lots_deployed=1,
        total_quantity=position.get("quantity", 1),
        margin_blocked=position.get("defined_loss", 0.0),
    )
    closed = dict(position)
    closed.update(mark)
    closed.update({"active": False, "exit_reason": exit_reason, "closed_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    return closed


def has_credit_sweep_entry_today(symbol: str, ledger_path: str | None = None, now: dt.datetime | None = None) -> bool:
    ledger_path = ledger_path or os.path.join(BASE_DIR, "sandbox_trade_logs.csv")
    now = now or dt.datetime.now()
    if not os.path.exists(ledger_path):
        return False
    try:
        df = pd.read_csv(ledger_path)
    except Exception:
        return False
    if df.empty or not {"Timestamp", "Action", "Index", "Strategy_Type"}.issubset(df.columns):
        return False
    ts = pd.to_datetime(df["Timestamp"], errors="coerce")
    mask = (
        (ts.dt.date.astype(str) == now.strftime("%Y-%m-%d"))
        & (df["Action"].astype(str).str.upper() == "ENTRY")
        & (df["Index"].astype(str).str.upper() == symbol.upper())
        & (df["Strategy_Type"].astype(str).str.upper() == STRATEGY_PAPER)
    )
    return bool(mask.any())
