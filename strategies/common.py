from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from base_strategy import Candle, TradeRecord


@dataclass
class OpenPosition:
    symbol: str
    side: str
    quantity: int
    entry_time: datetime
    entry_price: float
    stop_loss: float
    target_price: float
    order_id: str = ""
    best_stage: int = 0
    metadata: Dict[str, float] = None

    def __post_init__(self) -> None:
        if self.metadata is None:
            self.metadata = {}


def pnl_for_exit(position: OpenPosition, exit_price: float) -> float:
    direction = 1.0 if position.side.upper() == "BUY" else -1.0
    return (float(exit_price) - position.entry_price) * direction * position.quantity


def build_trade_record(
    strategy_name: str,
    position: OpenPosition,
    exit_time: datetime,
    exit_price: float,
    slippage: float = 0.0,
    reason: str = "",
) -> TradeRecord:
    metadata = dict(position.metadata or {})
    metadata["exit_reason"] = reason
    return TradeRecord(
        strategy_name=strategy_name,
        symbol=position.symbol,
        entry_time=position.entry_time,
        exit_time=exit_time,
        side=position.side,
        quantity=position.quantity,
        entry_price=position.entry_price,
        exit_price=exit_price,
        pnl=pnl_for_exit(position, exit_price),
        slippage=slippage,
        metadata=metadata,
    )


def previous_session_levels(candles: List[Candle]) -> Tuple[Optional[float], Optional[float]]:
    if len(candles) < 2:
        return None, None
    current_date = candles[-1].timestamp.date()
    prior = [c for c in candles if c.timestamp.date() < current_date]
    if not prior:
        return None, None
    prior_date = prior[-1].timestamp.date()
    day = [c for c in prior if c.timestamp.date() == prior_date]
    return max(c.high for c in day), min(c.low for c in day)


def opening_range_levels(candles: List[Candle], minutes: int = 15) -> Tuple[Optional[float], Optional[float]]:
    if not candles:
        return None, None
    current_date = candles[-1].timestamp.date()
    session = [c for c in candles if c.timestamp.date() == current_date]
    if not session:
        return None, None
    session_start = session[0].timestamp
    opening = [
        c for c in session
        if (c.timestamp - session_start).total_seconds() < minutes * 60
    ]
    if not opening:
        return None, None
    return max(c.high for c in opening), min(c.low for c in opening)


def stop_or_target_hit(position: OpenPosition, candle: Candle) -> Tuple[bool, Optional[str], Optional[float]]:
    if position.side.upper() == "BUY":
        stop_hit = candle.low <= position.stop_loss
        target_hit = candle.high >= position.target_price
    else:
        stop_hit = candle.high >= position.stop_loss
        target_hit = candle.low <= position.target_price

    if stop_hit and target_hit:
        return True, "same_candle_stop_priority", position.stop_loss
    if target_hit:
        return True, "target", position.target_price
    if stop_hit:
        return True, "stop", position.stop_loss
    return False, None, None
