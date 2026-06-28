from __future__ import annotations

from typing import Dict, Set, Tuple

import pandas as pd

import credit_sweep
from base_strategy import BaseStrategy, MarketDataEvent, OrderRequest, OrderUpdate
from strategies.common import OpenPosition, build_trade_record, stop_or_target_hit


class CreditSweepStrategy(BaseStrategy):
    """Framework wrapper for the paper-first Credit Sweep candidate."""

    async def on_initialize(self) -> None:
        self.primary_timeframe = str(self.config.get("timeframe", self.timeframes[0]))
        self.quantity = int(self.config.get("quantity", 1))
        self.positions: Dict[str, OpenPosition] = {}
        self.traded_days: Set[Tuple[str, str]] = set()
        self.logger.info(
            "STATE_CHANGE initialized symbols=%s timeframe=%s paper_only=true",
            ",".join(self.symbols),
            self.primary_timeframe,
        )

    async def on_market_data(self, data: MarketDataEvent) -> None:
        if data.timeframe != self.primary_timeframe:
            return

        position = self.positions.get(data.symbol)
        if position:
            await self._manage_position(position, data)
            if data.symbol in self.positions:
                return

        trade_day = data.candle.timestamp.date().isoformat()
        if (data.symbol, trade_day) in self.traded_days:
            return

        candles = self.get_candles(data.symbol, self.primary_timeframe, lookback=350)
        if len(candles) < 50:
            return

        frame = pd.DataFrame(
            [
                {
                    "datetime": candle.timestamp,
                    "open": candle.open,
                    "high": candle.high,
                    "low": candle.low,
                    "close": candle.close,
                    "volume": candle.volume,
                    "oi": candle.open_interest,
                }
                for candle in candles
            ]
        )
        normalized = credit_sweep.normalize_candles(frame)
        levels = credit_sweep.prior_day_levels(normalized, trade_day)
        signal = credit_sweep.evaluate_credit_sweep_signal(
            data.symbol,
            normalized[normalized["date"] == trade_day],
            levels,
            now=data.received_at,
            interval_minutes=5,
        )
        if not signal.confirmed:
            if signal.reject_reason:
                self.logger.info(
                    "SETUP_REJECTED symbol=%s reason=%s status=%s",
                    data.symbol,
                    signal.reject_reason,
                    signal.status,
                )
            return

        side = "BUY" if signal.direction == "BULLISH" else "SELL"
        spread_type = credit_sweep.strategy_type_for_direction(signal.direction)
        self.logger.info(
            "ENTRY_TRIGGER symbol=%s direction=%s spread_type=%s score=%s entry=%.4f stop=%.4f target=%.4f",
            data.symbol,
            signal.direction,
            spread_type,
            signal.score,
            signal.entry_price,
            signal.stop_price,
            signal.target_price,
        )
        update = await self.place_order(
            OrderRequest(
                strategy_name=self.name,
                symbol=data.symbol,
                transaction_type=side,
                quantity=self.quantity,
                intended_price=signal.entry_price,
                stop_loss=signal.stop_price,
                target_price=signal.target_price,
                metadata={
                    "paper_only": True,
                    "spread_type": spread_type,
                    "score": signal.score,
                    "rr_target": signal.rr_target,
                    "risk_points": signal.risk_points,
                    "reward_points": signal.reward_points,
                },
            )
        )
        self.positions[data.symbol] = OpenPosition(
            symbol=data.symbol,
            side=side,
            quantity=self.quantity,
            entry_time=data.candle.close_time,
            entry_price=update.executed_price,
            stop_loss=signal.stop_price,
            target_price=signal.target_price,
            order_id=update.order_id,
            metadata={"score": float(signal.score), "spread_type": spread_type},
        )
        self.traded_days.add((data.symbol, trade_day))

    async def on_order_update(self, order: OrderUpdate) -> None:
        self.logger.info(
            "ORDER_UPDATE symbol=%s order_id=%s status=%s executed=%.4f metadata=%s",
            order.symbol,
            order.order_id,
            order.status,
            order.executed_price,
            order.metadata,
        )

    async def on_shutdown(self) -> None:
        for symbol, position in self.positions.items():
            self.logger.warning(
                "STATE_CHANGE shutdown_open_position symbol=%s side=%s entry=%.4f",
                symbol,
                position.side,
                position.entry_price,
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
