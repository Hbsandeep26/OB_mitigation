from __future__ import annotations

from typing import Dict, Optional

from base_strategy import (
    BaseStrategy,
    MarketDataEvent,
    OrderRequest,
    OrderUpdate,
    atr,
    closes,
    ema,
    highs,
    lows,
)
from strategies.common import OpenPosition, build_trade_record, stop_or_target_hit


class MtfTrendFollowingStrategy(BaseStrategy):
    """5-minute execution aligned with closed 1-hour trend state."""

    async def on_initialize(self) -> None:
        self.execution_timeframe = str(self.config.get("execution_timeframe", "5m"))
        self.trend_timeframe = str(self.config.get("trend_timeframe", "60m"))
        self.fast_ema_length = int(self.config.get("fast_ema_length", 20))
        self.slow_ema_length = int(self.config.get("slow_ema_length", 50))
        self.pullback_ema_length = int(self.config.get("pullback_ema_length", 20))
        self.risk_reward = float(self.config.get("risk_reward", 2.0))
        self.quantity = int(self.config.get("quantity", 1))
        self.htf_bias: Dict[str, str] = {symbol: "NEUTRAL" for symbol in self.symbols}
        self.positions: Dict[str, OpenPosition] = {}
        self.logger.info(
            "STATE_CHANGE initialized symbols=%s execution_tf=%s trend_tf=%s",
            ",".join(self.symbols),
            self.execution_timeframe,
            self.trend_timeframe,
        )

    async def on_market_data(self, data: MarketDataEvent) -> None:
        if data.timeframe == self.trend_timeframe:
            self._update_trend_bias(data.symbol)
            return

        if data.timeframe != self.execution_timeframe:
            return

        position = self.positions.get(data.symbol)
        if position:
            await self._manage_position(position, data)
            if data.symbol in self.positions:
                return

        direction = self.htf_bias.get(data.symbol, "NEUTRAL")
        if direction == "NEUTRAL":
            return

        candles = self.get_candles(data.symbol, self.execution_timeframe, lookback=80)
        if len(candles) < 30:
            return

        close_values = closes(candles)
        pullback_ema = ema(close_values, self.pullback_ema_length)
        atr_value = atr(candles, 14)
        if pullback_ema is None or atr_value is None:
            return

        latest = candles[-1]
        previous = candles[-2]
        if direction == "BULLISH":
            pullback_ok = latest.low <= pullback_ema and latest.close > pullback_ema
            trigger_ok = latest.close > max(highs(candles[-6:-1])) and latest.close > previous.close
            side = "BUY"
            stop = min(min(lows(candles[-6:])), latest.close - atr_value)
            risk = max(latest.close - stop, atr_value * 0.5)
            target = latest.close + risk * self.risk_reward
        else:
            pullback_ok = latest.high >= pullback_ema and latest.close < pullback_ema
            trigger_ok = latest.close < min(lows(candles[-6:-1])) and latest.close < previous.close
            side = "SELL"
            stop = max(max(highs(candles[-6:])), latest.close + atr_value)
            risk = max(stop - latest.close, atr_value * 0.5)
            target = latest.close - risk * self.risk_reward

        self.logger.info(
            "INDICATOR_SNAPSHOT symbol=%s htf_bias=%s close=%.4f pullback_ema=%.4f atr=%.4f "
            "pullback_ok=%s trigger_ok=%s",
            data.symbol,
            direction,
            latest.close,
            pullback_ema,
            atr_value,
            pullback_ok,
            trigger_ok,
        )
        if not pullback_ok or not trigger_ok:
            return

        self.logger.info(
            "ENTRY_TRIGGER symbol=%s direction=%s entry=%.4f stop=%.4f target=%.4f",
            data.symbol,
            direction,
            latest.close,
            stop,
            target,
        )
        update = await self.place_order(
            OrderRequest(
                strategy_name=self.name,
                symbol=data.symbol,
                transaction_type=side,
                quantity=self.quantity,
                intended_price=latest.close,
                stop_loss=stop,
                target_price=target,
                metadata={"htf_bias": direction, "strategy": "mtf_trend_following"},
            )
        )
        self.positions[data.symbol] = OpenPosition(
            symbol=data.symbol,
            side=side,
            quantity=self.quantity,
            entry_time=latest.close_time,
            entry_price=update.executed_price,
            stop_loss=stop,
            target_price=target,
            order_id=update.order_id,
            metadata={"htf_bias": direction},
        )

    async def on_order_update(self, order: OrderUpdate) -> None:
        self.logger.info(
            "ORDER_UPDATE symbol=%s order_id=%s status=%s executed=%.4f latency_ms=%.2f",
            order.symbol,
            order.order_id,
            order.status,
            order.executed_price,
            order.latency_ms,
        )

    async def on_shutdown(self) -> None:
        for symbol, position in self.positions.items():
            self.logger.warning(
                "STATE_CHANGE shutdown_open_position symbol=%s side=%s entry=%.4f",
                symbol,
                position.side,
                position.entry_price,
            )

    def _update_trend_bias(self, symbol: str) -> None:
        candles = self.get_candles(symbol, self.trend_timeframe, lookback=120)
        if len(candles) < self.slow_ema_length + 5:
            return
        close_values = closes(candles)
        fast_value = ema(close_values, self.fast_ema_length)
        slow_value = ema(close_values, self.slow_ema_length)
        latest = candles[-1]
        if fast_value is None or slow_value is None:
            return
        if fast_value > slow_value and latest.close > slow_value:
            bias = "BULLISH"
        elif fast_value < slow_value and latest.close < slow_value:
            bias = "BEARISH"
        else:
            bias = "NEUTRAL"
        previous = self.htf_bias.get(symbol, "NEUTRAL")
        self.htf_bias[symbol] = bias
        self.logger.info(
            "STATE_CHANGE htf_bias symbol=%s previous=%s current=%s htf_close=%.4f ema_fast=%.4f ema_slow=%.4f",
            symbol,
            previous,
            bias,
            latest.close,
            fast_value,
            slow_value,
        )

    async def _manage_position(self, position: OpenPosition, data: MarketDataEvent) -> None:
        hit, reason, exit_price = stop_or_target_hit(position, data.candle)
        if not hit or exit_price is None:
            return
        trade = build_trade_record(self.name, position, data.candle.close_time, exit_price, reason=reason or "")
        self.record_trade(trade)
        self.logger.info(
            "EXIT_TRIGGER symbol=%s reason=%s exit=%.4f pnl=%.4f",
            position.symbol,
            reason,
            exit_price,
            trade.pnl,
        )
        self.positions.pop(position.symbol, None)
