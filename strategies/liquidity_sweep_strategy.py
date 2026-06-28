from __future__ import annotations

from typing import Dict, Optional, Tuple

from base_strategy import (
    BaseStrategy,
    MarketDataEvent,
    OrderRequest,
    OrderUpdate,
    atr,
    highs,
    lows,
    session_vwap,
    sma,
    volumes,
)
from strategies.common import (
    OpenPosition,
    build_trade_record,
    opening_range_levels,
    previous_session_levels,
    stop_or_target_hit,
)


class LiquiditySweepStrategy(BaseStrategy):
    """Single-timeframe sweep, reclaim/reject, VWAP, BOS, volume strategy."""

    async def on_initialize(self) -> None:
        self.primary_timeframe = self.timeframes[0]
        self.positions: Dict[str, OpenPosition] = {}
        self.last_signal_ts: Dict[str, object] = {}
        self.risk_reward = float(self.config.get("risk_reward", 2.0))
        self.quantity = int(self.config.get("quantity", 1))
        self.swing_lookback = int(self.config.get("swing_lookback", 5))
        self.opening_range_minutes = int(self.config.get("opening_range_minutes", 15))
        self.volume_multiplier = float(self.config.get("volume_multiplier", 1.2))
        self.max_atr_pct = float(self.config.get("max_atr_pct", 0.018))
        self.min_score = int(self.config.get("min_score", 75))
        self.logger.info(
            "STATE_CHANGE initialized symbols=%s timeframe=%s rr=%.2f min_score=%s",
            ",".join(self.symbols),
            self.primary_timeframe,
            self.risk_reward,
            self.min_score,
        )

    async def on_market_data(self, data: MarketDataEvent) -> None:
        if data.timeframe != self.primary_timeframe:
            return

        position = self.positions.get(data.symbol)
        if position:
            await self._manage_position(position, data)
            if data.symbol in self.positions:
                return

        signal = self._detect_signal(data.symbol)
        if not signal:
            return

        direction, entry, stop, target, score, indicators = signal
        side = "BUY" if direction == "BULLISH" else "SELL"
        self.logger.info(
            "ENTRY_TRIGGER symbol=%s direction=%s score=%s entry=%.4f stop=%.4f target=%.4f "
            "indicators=%s",
            data.symbol,
            direction,
            score,
            entry,
            stop,
            target,
            indicators,
        )
        update = await self.place_order(
            OrderRequest(
                strategy_name=self.name,
                symbol=data.symbol,
                transaction_type=side,
                quantity=self.quantity,
                intended_price=entry,
                target_price=target,
                stop_loss=stop,
                metadata={"direction": direction, "score": score, "strategy": "liquidity_sweep"},
            )
        )
        self.positions[data.symbol] = OpenPosition(
            symbol=data.symbol,
            side=side,
            quantity=self.quantity,
            entry_time=data.candle.close_time,
            entry_price=update.executed_price,
            stop_loss=stop,
            target_price=target,
            order_id=update.order_id,
            metadata={"score": float(score)},
        )

    async def on_order_update(self, order: OrderUpdate) -> None:
        self.logger.info(
            "ORDER_UPDATE symbol=%s order_id=%s status=%s executed=%.4f slippage=%.4f",
            order.symbol,
            order.order_id,
            order.status,
            order.executed_price,
            order.slippage,
        )

    async def on_shutdown(self) -> None:
        for symbol, position in list(self.positions.items()):
            self.logger.warning(
                "STATE_CHANGE shutdown_open_position symbol=%s side=%s entry=%.4f stop=%.4f target=%.4f",
                symbol,
                position.side,
                position.entry_price,
                position.stop_loss,
                position.target_price,
            )

    async def _manage_position(self, position: OpenPosition, data: MarketDataEvent) -> None:
        hit, reason, exit_price = stop_or_target_hit(position, data.candle)
        if not hit or exit_price is None:
            return
        trade = build_trade_record(
            self.name,
            position,
            data.candle.close_time,
            exit_price,
            reason=reason or "",
        )
        self.record_trade(trade)
        self.logger.info(
            "EXIT_TRIGGER symbol=%s reason=%s exit=%.4f pnl=%.4f",
            position.symbol,
            reason,
            exit_price,
            trade.pnl,
        )
        self.positions.pop(position.symbol, None)

    def _detect_signal(
        self,
        symbol: str,
    ) -> Optional[Tuple[str, float, float, float, int, Dict[str, float]]]:
        candles = self.get_candles(symbol, self.primary_timeframe, lookback=120)
        if len(candles) < max(30, self.swing_lookback + 5):
            return None

        latest = candles[-1]
        prior_high, prior_low = previous_session_levels(candles)
        opening_high, opening_low = opening_range_levels(candles, self.opening_range_minutes)
        if prior_high is None or prior_low is None or opening_high is None or opening_low is None:
            return None

        vwap_value = session_vwap(candles)
        atr_value = atr(candles, 14)
        volume_average = sma(volumes(candles[:-1]), 20)
        if vwap_value is None or atr_value is None or volume_average is None:
            return None

        bos_high = max(highs(candles[-self.swing_lookback - 1 : -1]))
        bos_low = min(lows(candles[-self.swing_lookback - 1 : -1]))
        bull_level = min(prior_low, opening_low)
        bear_level = max(prior_high, opening_high)
        volume_expansion = latest.volume >= volume_average * self.volume_multiplier
        atr_pct = atr_value / latest.close if latest.close else 99.0
        atr_ok = atr_pct <= self.max_atr_pct

        bullish = (
            latest.low < bull_level
            and latest.close > bull_level
            and latest.close > bos_high
            and latest.close > vwap_value
            and volume_expansion
            and atr_ok
        )
        bearish = (
            latest.high > bear_level
            and latest.close < bear_level
            and latest.close < bos_low
            and latest.close < vwap_value
            and volume_expansion
            and atr_ok
        )
        if not bullish and not bearish:
            return None

        score = 55
        score += 15 if volume_expansion else 0
        score += 10 if atr_ok else 0
        score += 10 if (latest.close > vwap_value if bullish else latest.close < vwap_value) else 0
        score += 10 if (latest.close > bos_high if bullish else latest.close < bos_low) else 0
        score = min(100, score)
        if score < self.min_score:
            self.logger.info(
                "SETUP_REJECTED symbol=%s reason=score_below_threshold score=%s min_score=%s",
                symbol,
                score,
                self.min_score,
            )
            return None

        if bullish:
            entry = latest.close
            stop = min(latest.low, bull_level)
            risk = max(entry - stop, atr_value * 0.35)
            target = entry + risk * self.risk_reward
            direction = "BULLISH"
        else:
            entry = latest.close
            stop = max(latest.high, bear_level)
            risk = max(stop - entry, atr_value * 0.35)
            target = entry - risk * self.risk_reward
            direction = "BEARISH"

        indicators = {
            "vwap": round(vwap_value, 4),
            "atr": round(atr_value, 4),
            "atr_pct": round(atr_pct, 6),
            "volume": round(latest.volume, 2),
            "volume_average": round(volume_average, 2),
            "bos_high": round(bos_high, 4),
            "bos_low": round(bos_low, 4),
        }
        self.logger.info(
            "SETUP_CONDITIONS_MET symbol=%s direction=%s score=%s indicators=%s",
            symbol,
            direction,
            score,
            indicators,
        )
        return direction, entry, stop, target, score, indicators
