from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

import pandas as pd

from base_strategy import Candle, MarketDataEvent, timeframe_seconds


def timeframe_minutes(timeframe: str) -> int:
    return timeframe_seconds(timeframe) // 60


def _parse_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    return pd.to_datetime(value).to_pydatetime()


@dataclass(frozen=True)
class Subscription:
    strategy_name: str
    symbols: Tuple[str, ...]
    timeframes: Tuple[str, ...]
    queue: asyncio.Queue


class HistoricalCsvDataProvider:
    """Closed-candle provider backed by data/historical CSV files.

    Exact timeframe files are preferred. If a higher timeframe file is absent,
    the provider resamples the best available lower timeframe file without
    exposing partial candles.
    """

    def __init__(self, data_dir: str = "data/historical") -> None:
        self.data_dir = Path(data_dir)
        self._cache: Dict[Tuple[str, str], List[Candle]] = {}

    def candles(self, symbol: str, timeframe: str) -> List[Candle]:
        key = (symbol.upper(), timeframe)
        if key not in self._cache:
            self._cache[key] = self._load_or_resample(symbol.upper(), timeframe)
        return list(self._cache[key])

    def _load_or_resample(self, symbol: str, timeframe: str) -> List[Candle]:
        exact = self.data_dir / f"{symbol}_{timeframe}.csv"
        if exact.exists():
            return self._frame_to_candles(symbol, timeframe, self._read_frame(exact))

        target_minutes = timeframe_minutes(timeframe)
        candidates = sorted(self.data_dir.glob(f"{symbol}_*.csv"))
        lower_candidates: List[Tuple[int, Path]] = []
        for path in candidates:
            source_timeframe = path.stem.replace(f"{symbol}_", "")
            try:
                source_minutes = timeframe_minutes(source_timeframe)
            except ValueError:
                continue
            if source_minutes < target_minutes and target_minutes % source_minutes == 0:
                lower_candidates.append((source_minutes, path))

        if not lower_candidates:
            raise FileNotFoundError(f"No CSV source found for {symbol} {timeframe}")

        _, source_path = max(lower_candidates, key=lambda item: item[0])
        frame = self._read_frame(source_path)
        resampled = self._resample_frame(frame, timeframe)
        return self._frame_to_candles(symbol, timeframe, resampled)

    def _read_frame(self, path: Path) -> pd.DataFrame:
        frame = pd.read_csv(path)
        if "datetime" in frame.columns:
            dt_series = pd.to_datetime(frame["datetime"])
        elif "timestamp" in frame.columns:
            dt_series = pd.to_datetime(frame["timestamp"], unit="s", errors="coerce")
            if dt_series.isna().all():
                dt_series = pd.to_datetime(frame["timestamp"])
        else:
            raise ValueError(f"{path} must include datetime or timestamp")

        frame = frame.copy()
        frame["datetime"] = dt_series
        numeric_cols = ["open", "high", "low", "close", "volume", "open_interest"]
        for column in numeric_cols:
            if column not in frame.columns:
                frame[column] = 0.0
            frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)
        frame = frame.dropna(subset=["datetime"]).sort_values("datetime")
        return frame

    def _resample_frame(self, frame: pd.DataFrame, timeframe: str) -> pd.DataFrame:
        minutes = timeframe_minutes(timeframe)
        indexed = frame.set_index("datetime")
        aggregation = {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
            "open_interest": "last",
        }
        resampled = indexed.resample(f"{minutes}min", label="left", closed="left").agg(aggregation)
        resampled = resampled.dropna(subset=["open", "high", "low", "close"])
        resampled = resampled.reset_index()
        return resampled

    def _frame_to_candles(self, symbol: str, timeframe: str, frame: pd.DataFrame) -> List[Candle]:
        candles: List[Candle] = []
        for row in frame.itertuples(index=False):
            candles.append(
                Candle(
                    symbol=symbol,
                    timeframe=timeframe,
                    timestamp=_parse_datetime(getattr(row, "datetime")),
                    open=float(getattr(row, "open")),
                    high=float(getattr(row, "high")),
                    low=float(getattr(row, "low")),
                    close=float(getattr(row, "close")),
                    volume=float(getattr(row, "volume", 0.0)),
                    open_interest=float(getattr(row, "open_interest", 0.0)),
                    is_closed=True,
                )
            )
        return candles


class AsyncMarketDataEngine:
    def __init__(
        self,
        provider: HistoricalCsvDataProvider,
        replay_sleep_seconds: float = 0.0,
    ) -> None:
        self.provider = provider
        self.replay_sleep_seconds = float(replay_sleep_seconds)
        self._subscriptions: List[Subscription] = []

    def register_strategy(
        self,
        strategy_name: str,
        symbols: Iterable[str],
        timeframes: Iterable[str],
        queue: asyncio.Queue,
    ) -> None:
        self._subscriptions.append(
            Subscription(
                strategy_name=strategy_name,
                symbols=tuple(symbol.upper() for symbol in symbols),
                timeframes=tuple(str(tf) for tf in timeframes),
                queue=queue,
            )
        )

    def required_feeds(self) -> Set[Tuple[str, str]]:
        feeds: Set[Tuple[str, str]] = set()
        for subscription in self._subscriptions:
            for symbol in subscription.symbols:
                for timeframe in subscription.timeframes:
                    feeds.add((symbol, timeframe))
        return feeds

    async def run_replay(self) -> None:
        events: List[MarketDataEvent] = []
        for symbol, timeframe in self.required_feeds():
            for candle in self.provider.candles(symbol, timeframe):
                events.append(
                    MarketDataEvent(
                        symbol=symbol,
                        timeframe=timeframe,
                        candle=candle,
                        received_at=candle.close_time,
                        is_closed=True,
                    )
                )

        events.sort(key=lambda event: (event.candle.close_time, timeframe_seconds(event.timeframe)))
        for event in events:
            await self.publish(event)
            if self.replay_sleep_seconds > 0:
                await asyncio.sleep(self.replay_sleep_seconds)

        for subscription in self._subscriptions:
            await subscription.queue.put(None)

    async def publish(self, event: MarketDataEvent) -> None:
        for subscription in self._subscriptions:
            if event.symbol in subscription.symbols and event.timeframe in subscription.timeframes:
                await subscription.queue.put(event)


class LiveMarketDataEngine:
    def __init__(self) -> None:
        self._subscriptions: List[Subscription] = []
        self._last_candle_times: Dict[Tuple[str, str], datetime] = {}
        self._stop = asyncio.Event()

    def register_strategy(
        self,
        strategy_name: str,
        symbols: Iterable[str],
        timeframes: Iterable[str],
        queue: asyncio.Queue,
    ) -> None:
        self._subscriptions.append(
            Subscription(
                strategy_name=strategy_name,
                symbols=tuple(symbol.upper() for symbol in symbols),
                timeframes=tuple(str(tf) for tf in timeframes),
                queue=queue,
            )
        )

    def required_feeds(self) -> Set[Tuple[str, str]]:
        feeds: Set[Tuple[str, str]] = set()
        for subscription in self._subscriptions:
            for symbol in subscription.symbols:
                for timeframe in subscription.timeframes:
                    feeds.add((symbol, timeframe))
        return feeds

    async def publish(self, event: MarketDataEvent) -> None:
        for subscription in self._subscriptions:
            if event.symbol in subscription.symbols and event.timeframe in subscription.timeframes:
                await subscription.queue.put(event)

    async def run_live(self) -> None:
        from broker import get_broker
        import logging
        import datetime
        
        logger = logging.getLogger("master_engine")
        logger.info("Initializing LiveMarketDataEngine...")
        
        # 1. Bootstrapping
        broker = get_broker()
        now = datetime.datetime.now()
        # Fetch last 5 calendar days to get enough intraday candles for lookback indicators
        from_date = now - datetime.timedelta(days=5)
        
        logger.info("Bootstrapping historical candles for required feeds since %s...", from_date)
        for symbol, timeframe in self.required_feeds():
            tf_minutes = timeframe_minutes(timeframe)
            try:
                logger.info("Bootstrapping %s %s candles from broker...", symbol, timeframe)
                raw_candles = broker.get_intraday_candles(symbol, minutes=tf_minutes, from_date=from_date)
                logger.info("Retrieved %s bootstrap candles for %s %s", len(raw_candles), symbol, timeframe)
                
                # Convert raw_candles to Candle objects
                candles: List[Candle] = []
                for rc in raw_candles:
                    ts = _parse_datetime(rc[0])
                    # Ensure we only bootstrap fully closed candles
                    candle_close_time = ts + datetime.timedelta(minutes=tf_minutes)
                    if datetime.datetime.now() >= candle_close_time:
                        candles.append(
                            Candle(
                                symbol=symbol,
                                timeframe=timeframe,
                                timestamp=ts,
                                open=float(rc[1]),
                                high=float(rc[2]),
                                low=float(rc[3]),
                                close=float(rc[4]),
                                volume=float(rc[5]) if len(rc) > 5 else 0.0,
                                open_interest=float(rc[6]) if len(rc) > 6 else 0.0,
                                is_closed=True,
                            )
                        )
                
                # Publish bootstrap candles to strategies in chronological order
                for candle in candles:
                    event = MarketDataEvent(
                        symbol=symbol,
                        timeframe=timeframe,
                        candle=candle,
                        received_at=candle.close_time,
                        is_closed=True,
                    )
                    await self.publish(event)
                    
                if candles:
                    self._last_candle_times[(symbol, timeframe)] = max(c.timestamp for c in candles)
                    logger.info("Bootstrapped %s %s. Last candle timestamp: %s", symbol, timeframe, self._last_candle_times[(symbol, timeframe)])
            except Exception as e:
                logger.error("Failed to bootstrap %s %s: %s", symbol, timeframe, e)

        # 2. Polling Loop
        logger.info("Live data bootstrapping complete. Entering real-time candle polling loop.")
        while not self._stop.is_set():
            now = datetime.datetime.now()
            today_morning = now.replace(hour=9, minute=15, second=0, microsecond=0)
            
            for symbol, timeframe in self.required_feeds():
                tf_minutes = timeframe_minutes(timeframe)
                try:
                    last_time = self._last_candle_times.get((symbol, timeframe))
                    # Go back slightly in case of late closed candles
                    fetch_start = last_time - datetime.timedelta(minutes=tf_minutes * 2) if last_time else today_morning
                    
                    raw_candles = broker.get_intraday_candles(symbol, minutes=tf_minutes, from_date=fetch_start)
                    new_candles_count = 0
                    
                    for rc in raw_candles:
                        ts = _parse_datetime(rc[0])
                        candle_close_time = ts + datetime.timedelta(minutes=tf_minutes)
                        
                        # Only publish closed candles strictly after the last pushed candle
                        if datetime.datetime.now() >= candle_close_time:
                            if last_time is None or ts > last_time:
                                candle = Candle(
                                    symbol=symbol,
                                    timeframe=timeframe,
                                    timestamp=ts,
                                    open=float(rc[1]),
                                    high=float(rc[2]),
                                    low=float(rc[3]),
                                    close=float(rc[4]),
                                    volume=float(rc[5]) if len(rc) > 5 else 0.0,
                                    open_interest=float(rc[6]) if len(rc) > 6 else 0.0,
                                    is_closed=True,
                                )
                                event = MarketDataEvent(
                                    symbol=symbol,
                                    timeframe=timeframe,
                                    candle=candle,
                                    received_at=candle.close_time,
                                    is_closed=True,
                                )
                                await self.publish(event)
                                self._last_candle_times[(symbol, timeframe)] = ts
                                last_time = ts
                                new_candles_count += 1
                    
                    if new_candles_count > 0:
                        logger.info("Published %s new closed %s %s candles. Latest time: %s", new_candles_count, symbol, timeframe, last_time)
                except Exception as e:
                    logger.error("Error polling live %s %s: %s", symbol, timeframe, e)
                
                # Prevent rate limit hit (HTTP 429)
                await asyncio.sleep(1.0)
                
            # Sleep 10 seconds before next polling cycle
            await asyncio.sleep(10.0)

    def stop(self) -> None:
        self._stop.set()


def build_data_provider(engine_config: Dict[str, object]) -> HistoricalCsvDataProvider:
    data_config = dict(engine_config.get("data", {}))
    data_dir = str(data_config.get("data_dir", os.path.join("data", "historical")))
    return HistoricalCsvDataProvider(data_dir=data_dir)
