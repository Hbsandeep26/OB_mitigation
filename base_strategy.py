from __future__ import annotations

import asyncio
import logging
import math
import time
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Deque, Dict, Iterable, List, Optional


TIMEFRAME_SECONDS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "10m": 600,
    "15m": 900,
    "30m": 1800,
    "60m": 3600,
    "1h": 3600,
}


def timeframe_seconds(timeframe: str) -> int:
    normalized = str(timeframe).strip().lower()
    if normalized not in TIMEFRAME_SECONDS:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    return TIMEFRAME_SECONDS[normalized]


@dataclass(frozen=True)
class Candle:
    symbol: str
    timeframe: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    open_interest: float = 0.0
    is_closed: bool = True

    @property
    def close_time(self) -> datetime:
        return self.timestamp + timedelta(seconds=timeframe_seconds(self.timeframe))


@dataclass(frozen=True)
class MarketDataEvent:
    symbol: str
    timeframe: str
    candle: Candle
    received_at: datetime
    is_closed: bool = True


@dataclass
class OrderRequest:
    strategy_name: str
    symbol: str
    transaction_type: str
    quantity: int
    order_type: str = "MARKET"
    intended_price: float = 0.0
    target_price: Optional[float] = None
    stop_loss: Optional[float] = None
    trigger_price: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderUpdate:
    strategy_name: str
    symbol: str
    order_id: str
    transaction_type: str
    quantity: int
    status: str
    intended_price: float
    executed_price: float
    target_price: Optional[float] = None
    stop_loss: Optional[float] = None
    trigger_price: Optional[float] = None
    slippage: float = 0.0
    latency_ms: float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TradeRecord:
    strategy_name: str
    symbol: str
    entry_time: datetime
    exit_time: datetime
    side: str
    quantity: int
    entry_price: float
    exit_price: float
    pnl: float
    slippage: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StrategyContext:
    name: str
    symbols: List[str]
    timeframes: List[str]
    mode: str
    logger: logging.Logger
    order_router: Any
    performance_tracker: Any
    config: Dict[str, Any] = field(default_factory=dict)


def closes(candles: Iterable[Candle]) -> List[float]:
    return [float(c.close) for c in candles]


def highs(candles: Iterable[Candle]) -> List[float]:
    return [float(c.high) for c in candles]


def lows(candles: Iterable[Candle]) -> List[float]:
    return [float(c.low) for c in candles]


def volumes(candles: Iterable[Candle]) -> List[float]:
    return [float(c.volume) for c in candles]


def sma(values: Iterable[float], length: int) -> Optional[float]:
    data = list(values)
    if length <= 0 or len(data) < length:
        return None
    window = data[-length:]
    return sum(window) / float(length)


def ema_series(values: Iterable[float], length: int) -> List[float]:
    data = [float(v) for v in values]
    if not data or length <= 0:
        return []
    alpha = 2.0 / (length + 1.0)
    output = [data[0]]
    for value in data[1:]:
        output.append((value * alpha) + (output[-1] * (1.0 - alpha)))
    return output


def ema(values: Iterable[float], length: int) -> Optional[float]:
    series = ema_series(values, length)
    return series[-1] if series else None


def true_ranges(candles: List[Candle]) -> List[float]:
    ranges: List[float] = []
    previous_close: Optional[float] = None
    for candle in candles:
        if previous_close is None:
            ranges.append(float(candle.high) - float(candle.low))
        else:
            ranges.append(
                max(
                    float(candle.high) - float(candle.low),
                    abs(float(candle.high) - previous_close),
                    abs(float(candle.low) - previous_close),
                )
            )
        previous_close = float(candle.close)
    return ranges


def atr(candles: List[Candle], length: int = 14) -> Optional[float]:
    if len(candles) < length + 1:
        return None
    return sma(true_ranges(candles), length)


def rsi(values: Iterable[float], length: int = 14) -> Optional[float]:
    data = [float(v) for v in values]
    if len(data) < length + 1:
        return None
    gains: List[float] = []
    losses: List[float] = []
    for left, right in zip(data[-length - 1 : -1], data[-length:]):
        change = right - left
        gains.append(max(change, 0.0))
        losses.append(abs(min(change, 0.0)))
    average_gain = sum(gains) / float(length)
    average_loss = sum(losses) / float(length)
    if average_loss == 0:
        return 100.0
    rs_value = average_gain / average_loss
    return 100.0 - (100.0 / (1.0 + rs_value))


def session_vwap(candles: List[Candle]) -> Optional[float]:
    if not candles:
        return None
    session_date = candles[-1].timestamp.date()
    session_candles = [c for c in candles if c.timestamp.date() == session_date]
    total_volume = sum(max(float(c.volume), 0.0) for c in session_candles)
    if total_volume <= 0:
        return None
    typical_price_volume = sum(
        ((float(c.high) + float(c.low) + float(c.close)) / 3.0) * max(float(c.volume), 0.0)
        for c in session_candles
    )
    return typical_price_volume / total_volume


def max_drawdown_from_equity(equity_curve: Iterable[float]) -> float:
    peak = -math.inf
    max_dd = 0.0
    for value in equity_curve:
        peak = max(peak, float(value))
        max_dd = min(max_dd, float(value) - peak)
    return abs(max_dd)


class BaseStrategy(ABC):
    """Strict strategy interface plus closed-candle MTF state management."""

    def __init__(self, context: StrategyContext) -> None:
        self.context = context
        self.name = context.name
        self.symbols = context.symbols
        self.timeframes = context.timeframes
        self.logger = context.logger
        self.config = context.config
        self._candles: Dict[str, Dict[str, Deque[Candle]]] = defaultdict(
            lambda: defaultdict(lambda: deque(maxlen=int(self.config.get("max_candles", 1500))))
        )
        self._state_lock = asyncio.Lock()

    async def update_timeframe_state(self, event: MarketDataEvent) -> bool:
        """Append only fully closed candles to avoid live look-ahead bias."""

        if not event.is_closed or not event.candle.is_closed:
            return False

        async with self._state_lock:
            candle_queue = self._candles[event.symbol][event.timeframe]
            if candle_queue and candle_queue[-1].timestamp == event.candle.timestamp:
                candle_queue[-1] = event.candle
            elif not candle_queue or event.candle.timestamp > candle_queue[-1].timestamp:
                candle_queue.append(event.candle)
            else:
                self.logger.warning(
                    "STALE_CANDLE_IGNORED symbol=%s timeframe=%s candle_ts=%s last_ts=%s",
                    event.symbol,
                    event.timeframe,
                    event.candle.timestamp.isoformat(),
                    candle_queue[-1].timestamp.isoformat(),
                )
                return False
        return True

    def get_candles(self, symbol: str, timeframe: str, lookback: Optional[int] = None) -> List[Candle]:
        data = list(self._candles.get(symbol, {}).get(timeframe, []))
        return data[-lookback:] if lookback else data

    async def place_order(self, request: OrderRequest) -> OrderUpdate:
        start = time.perf_counter()
        update = await self.context.order_router.place_order(request)
        update.latency_ms = (time.perf_counter() - start) * 1000.0
        self.logger.info(
            "ORDER_METADATA symbol=%s order_id=%s side=%s qty=%s intended=%.4f executed=%.4f "
            "target=%s stop=%s trigger=%s slippage=%.4f latency_ms=%.2f",
            update.symbol,
            update.order_id,
            update.transaction_type,
            update.quantity,
            update.intended_price,
            update.executed_price,
            update.target_price,
            update.stop_loss,
            update.trigger_price,
            update.slippage,
            update.latency_ms,
        )
        await self.on_order_update(update)
        return update

    def record_trade(self, trade: TradeRecord) -> None:
        self.context.performance_tracker.record_trade(self.name, trade)

    @abstractmethod
    async def on_initialize(self) -> None:
        """Set up indicators, tokens, state, and capital allocation."""

    @abstractmethod
    async def on_market_data(self, data: MarketDataEvent) -> None:
        """Handle live or replayed closed candles/ticks."""

    @abstractmethod
    async def on_order_update(self, order: OrderUpdate) -> None:
        """Handle order placement and execution feedback."""

    @abstractmethod
    async def on_shutdown(self) -> None:
        """Gracefully close or mark open positions before engine shutdown."""
