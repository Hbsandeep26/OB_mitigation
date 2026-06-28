from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from base_strategy import (
    BaseStrategy,
    Candle,
    MarketDataEvent,
    OrderRequest,
    OrderUpdate,
    atr,
    closes,
    ema_series,
    highs,
    lows,
    rsi,
    sma,
    volumes,
)
from strategies.common import OpenPosition, build_trade_record


class PullbackSniperStrategy(BaseStrategy):
    """Python plug-in version of the provided Pullback Sniper Pine logic."""

    async def on_initialize(self) -> None:
        self.primary_timeframe = str(self.config.get("timeframe", self.timeframes[0]))
        self.fast_ema_length = int(self.config.get("fast_ema_length", 50))
        self.slow_ema_length = int(self.config.get("slow_ema_length", 200))
        self.pullback_ema_length = int(self.config.get("pullback_ema_length", 21))
        self.slope_lookback = int(self.config.get("slope_lookback", 5))
        self.breakout_lookback = int(self.config.get("breakout_lookback", 20))
        self.invalidation_lookback = int(self.config.get("invalidation_lookback", 12))
        self.min_bars_after_breakout = int(self.config.get("min_bars_after_breakout", 2))
        self.max_bars_to_find_pullback = int(self.config.get("max_bars_to_find_pullback", 60))
        self.confirmation_mode = str(self.config.get("confirmation_mode", "Balanced"))
        self.atr_length = int(self.config.get("atr_length", 14))
        self.min_breakout_body_atr = float(self.config.get("min_breakout_body_atr", 0.20))
        self.min_confirm_body_atr = float(self.config.get("min_confirm_body_atr", 0.25))
        self.max_entry_distance_atr = float(self.config.get("max_entry_distance_atr", 1.00))
        self.use_cooldown = bool(self.config.get("use_cooldown", True))
        self.cooldown_bars = int(self.config.get("cooldown_bars", 20))
        self.use_mcginley_filter = bool(self.config.get("use_mcginley_filter", True))
        self.mcginley_length = int(self.config.get("mcginley_length", 100))
        self.mcginley_distance_atr = float(self.config.get("mcginley_distance_atr", 0.25))
        self.use_rsi_filter = bool(self.config.get("use_rsi_filter", False))
        self.rsi_length = int(self.config.get("rsi_length", 14))
        self.rsi_long_min = float(self.config.get("rsi_long_min", 50.0))
        self.rsi_short_max = float(self.config.get("rsi_short_max", 50.0))
        self.trade_atr_length = int(self.config.get("trade_atr_length", 14))
        self.trade_atr_multiplier = float(self.config.get("trade_atr_multiplier", 2.0))
        self.tp3_reward_r = float(self.config.get("tp3_reward_r", 2.0))
        self.tp1_percent_of_tp3 = float(self.config.get("tp1_percent_of_tp3", 25.0))
        self.tp2_percent_of_tp3 = float(self.config.get("tp2_percent_of_tp3", 50.0))
        self.quantity = int(self.config.get("quantity", 1))
        self.min_quality_score = int(self.config.get("min_quality_score", 70))
        self.weak_pullback_volume_multiplier = float(
            self.config.get("weak_pullback_volume_multiplier", 1.35)
        )
        self.positions: Dict[str, OpenPosition] = {}
        self.state = defaultdict(self._new_state)
        self.logger.info(
            "STATE_CHANGE initialized symbols=%s timeframe=%s mode=%s",
            ",".join(self.symbols),
            self.primary_timeframe,
            self.confirmation_mode,
        )

    async def on_market_data(self, data: MarketDataEvent) -> None:
        if data.timeframe != self.primary_timeframe:
            return

        position = self.positions.get(data.symbol)
        if position:
            await self._manage_position(position, data)
            if data.symbol in self.positions:
                return

        candles = self.get_candles(data.symbol, self.primary_timeframe)
        minimum = max(
            self.slow_ema_length + self.slope_lookback + 2,
            self.breakout_lookback + 3,
            self.mcginley_length + 2,
        )
        if len(candles) < minimum:
            return

        signal = self._evaluate_signal(data.symbol, candles)
        if not signal:
            return

        direction, score, stop, target, tp1, tp2, indicators = signal
        side = "BUY" if direction == "LONG" else "SELL"
        latest = candles[-1]
        self.logger.info(
            "ENTRY_TRIGGER symbol=%s direction=%s score=%s entry=%.4f stop=%.4f "
            "target=%.4f indicators=%s",
            data.symbol,
            direction,
            score,
            latest.close,
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
                intended_price=latest.close,
                stop_loss=stop,
                target_price=target,
                metadata={
                    "strategy": "pullback_sniper",
                    "direction": direction,
                    "score": score,
                    "tp1": tp1,
                    "tp2": tp2,
                    "tp3": target,
                },
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
            metadata={"score": float(score), "tp1": tp1, "tp2": tp2, "tp3": target},
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
        for symbol, position in self.positions.items():
            self.logger.warning(
                "STATE_CHANGE shutdown_open_position symbol=%s side=%s entry=%.4f best_stage=%s",
                symbol,
                position.side,
                position.entry_price,
                position.best_stage,
            )

    def _new_state(self) -> Dict[str, object]:
        return {
            "setup_active": False,
            "setup_direction": 0,
            "setup_start_bar": None,
            "setup_invalidation": None,
            "setup_pullback_hit": False,
            "last_signal_bar": None,
            "tp3_direction_lock": 0,
        }

    def _evaluate_signal(
        self,
        symbol: str,
        candles: List[Candle],
    ) -> Optional[Tuple[str, int, float, float, float, float, Dict[str, float]]]:
        state = self.state[symbol]
        bar_index = len(candles) - 1
        latest = candles[-1]
        previous = candles[-2]
        close_values = closes(candles)
        fast_ema_values = ema_series(close_values, self.fast_ema_length)
        slow_ema_values = ema_series(close_values, self.slow_ema_length)
        pullback_ema_values = ema_series(close_values, self.pullback_ema_length)
        atr_value = atr(candles, self.atr_length)
        trade_atr = atr(candles, self.trade_atr_length)
        rsi_value = rsi(close_values, self.rsi_length) or 50.0
        if not fast_ema_values or not slow_ema_values or not pullback_ema_values or atr_value is None or trade_atr is None:
            return None

        fast_ema = fast_ema_values[-1]
        slow_ema = slow_ema_values[-1]
        pullback_ema = pullback_ema_values[-1]
        fast_ema_prior = fast_ema_values[-1 - self.slope_lookback]
        body_size = abs(latest.close - latest.open)
        upper_wick = latest.high - max(latest.open, latest.close)
        lower_wick = min(latest.open, latest.close) - latest.low

        bull_trend = fast_ema > slow_ema and latest.close > slow_ema and fast_ema > fast_ema_prior
        bear_trend = fast_ema < slow_ema and latest.close < slow_ema and fast_ema < fast_ema_prior
        highest_before = max(highs(candles[-self.breakout_lookback - 1 : -1]))
        lowest_before = min(lows(candles[-self.breakout_lookback - 1 : -1]))
        breakout_body_ok = body_size >= atr_value * self.min_breakout_body_atr

        bull_breakout = (
            bull_trend and latest.close > highest_before and latest.close > latest.open and breakout_body_ok
        )
        bear_breakout = (
            bear_trend and latest.close < lowest_before and latest.close < latest.open and breakout_body_ok
        )
        can_create_setup = not self.positions.get(symbol) and not state["setup_active"]
        if bull_breakout and can_create_setup:
            state.update(
                {
                    "setup_active": True,
                    "setup_direction": 1,
                    "setup_start_bar": bar_index,
                    "setup_invalidation": min(lows(candles[-self.invalidation_lookback - 1 : -1])),
                    "setup_pullback_hit": False,
                }
            )
            self.logger.info("STATE_CHANGE setup_created symbol=%s direction=LONG", symbol)
        elif bear_breakout and can_create_setup:
            state.update(
                {
                    "setup_active": True,
                    "setup_direction": -1,
                    "setup_start_bar": bar_index,
                    "setup_invalidation": max(highs(candles[-self.invalidation_lookback - 1 : -1])),
                    "setup_pullback_hit": False,
                }
            )
            self.logger.info("STATE_CHANGE setup_created symbol=%s direction=SHORT", symbol)

        if not state["setup_active"]:
            return None

        setup_start = int(state["setup_start_bar"] or bar_index)
        bars_since_setup = bar_index - setup_start
        invalidation = float(state["setup_invalidation"] or latest.close)
        setup_expired = bars_since_setup > self.max_bars_to_find_pullback
        setup_invalidated = (
            int(state["setup_direction"]) == 1 and latest.close < invalidation
        ) or (
            int(state["setup_direction"]) == -1 and latest.close > invalidation
        )
        if setup_expired or setup_invalidated:
            self.logger.info(
                "SETUP_REJECTED symbol=%s reason=%s bars_since_setup=%s",
                symbol,
                "expired" if setup_expired else "invalidated",
                bars_since_setup,
            )
            state.update(self._new_state())
            return None

        if int(state["setup_direction"]) == 1 and not state["setup_pullback_hit"]:
            if bars_since_setup >= self.min_bars_after_breakout and latest.low <= pullback_ema and latest.close > invalidation:
                state["setup_pullback_hit"] = True
                self.logger.info("STATE_CHANGE pullback_hit symbol=%s direction=LONG", symbol)
        elif int(state["setup_direction"]) == -1 and not state["setup_pullback_hit"]:
            if bars_since_setup >= self.min_bars_after_breakout and latest.high >= pullback_ema and latest.close < invalidation:
                state["setup_pullback_hit"] = True
                self.logger.info("STATE_CHANGE pullback_hit symbol=%s direction=SHORT", symbol)

        if not state["setup_pullback_hit"]:
            return None

        entry_distance_ok = abs(latest.close - pullback_ema) <= atr_value * self.max_entry_distance_atr
        confirm_body_ok = body_size >= atr_value * self.min_confirm_body_atr
        long_confirm = self._confirm_long(latest, previous, pullback_ema, lower_wick, body_size, confirm_body_ok)
        short_confirm = self._confirm_short(latest, previous, pullback_ema, upper_wick, body_size, confirm_body_ok)
        cooldown_allowed = (
            not self.use_cooldown
            or state["last_signal_bar"] is None
            or bar_index - int(state["last_signal_bar"]) >= self.cooldown_bars
        )
        mcginley_value = self._mcginley(close_values, self.mcginley_length)
        mcginley_distance = abs(latest.close - mcginley_value) if mcginley_value is not None else atr_value
        mcginley_allowed = (
            not self.use_mcginley_filter
            or mcginley_distance >= atr_value * self.mcginley_distance_atr
        )
        rsi_long_allowed = not self.use_rsi_filter or rsi_value >= self.rsi_long_min
        rsi_short_allowed = not self.use_rsi_filter or rsi_value <= self.rsi_short_max
        avg_volume = sma(volumes(candles[:-1]), 20)
        weak_pullback_ok = (
            avg_volume is None
            or latest.volume <= avg_volume * self.weak_pullback_volume_multiplier
            or latest.close > highest_before
            or latest.close < lowest_before
        )

        direction = int(state["setup_direction"])
        strong_long = (
            direction == 1
            and bull_trend
            and long_confirm
            and entry_distance_ok
            and mcginley_allowed
            and rsi_long_allowed
            and cooldown_allowed
            and weak_pullback_ok
        )
        strong_short = (
            direction == -1
            and bear_trend
            and short_confirm
            and entry_distance_ok
            and mcginley_allowed
            and rsi_short_allowed
            and cooldown_allowed
            and weak_pullback_ok
        )
        if not strong_long and not strong_short:
            return None

        is_long = strong_long
        score = self._quality_score(
            is_long,
            bull_trend,
            bear_trend,
            body_size,
            atr_value,
            latest.close,
            pullback_ema,
            lower_wick,
            upper_wick,
            mcginley_distance,
            rsi_value,
        )
        if score < self.min_quality_score:
            self.logger.info(
                "SETUP_REJECTED symbol=%s reason=score_below_threshold score=%s min_score=%s",
                symbol,
                score,
                self.min_quality_score,
            )
            return None

        if is_long:
            risk_distance = trade_atr * self.trade_atr_multiplier
            stop = latest.close - risk_distance
            tp1 = latest.close + risk_distance * self.tp3_reward_r * (self.tp1_percent_of_tp3 / 100.0)
            tp2 = latest.close + risk_distance * self.tp3_reward_r * (self.tp2_percent_of_tp3 / 100.0)
            tp3 = latest.close + risk_distance * self.tp3_reward_r
            signal_direction = "LONG"
        else:
            risk_distance = trade_atr * self.trade_atr_multiplier
            stop = latest.close + risk_distance
            tp1 = latest.close - risk_distance * self.tp3_reward_r * (self.tp1_percent_of_tp3 / 100.0)
            tp2 = latest.close - risk_distance * self.tp3_reward_r * (self.tp2_percent_of_tp3 / 100.0)
            tp3 = latest.close - risk_distance * self.tp3_reward_r
            signal_direction = "SHORT"

        state["last_signal_bar"] = bar_index
        state["setup_active"] = False
        state["setup_pullback_hit"] = False
        indicators = {
            "fast_ema": round(fast_ema, 4),
            "slow_ema": round(slow_ema, 4),
            "pullback_ema": round(pullback_ema, 4),
            "atr": round(atr_value, 4),
            "body_atr": round(body_size / atr_value if atr_value else 0.0, 4),
            "rsi": round(rsi_value, 2),
            "mcginley": round(mcginley_value or 0.0, 4),
            "mcginley_distance": round(mcginley_distance, 4),
        }
        self.logger.info(
            "SETUP_CONDITIONS_MET symbol=%s direction=%s score=%s indicators=%s",
            symbol,
            signal_direction,
            score,
            indicators,
        )
        return signal_direction, score, stop, tp3, tp1, tp2, indicators

    def _confirm_long(
        self,
        latest: Candle,
        previous: Candle,
        pullback_ema: float,
        lower_wick: float,
        body_size: float,
        confirm_body_ok: bool,
    ) -> bool:
        if self.confirmation_mode == "Fast":
            return latest.close > pullback_ema and latest.close > latest.open
        if self.confirmation_mode == "Strict":
            return latest.close > pullback_ema and latest.close > previous.high and latest.close > latest.open and confirm_body_ok
        return latest.close > pullback_ema and latest.close > latest.open and (
            latest.close > previous.high or lower_wick >= body_size * 0.50
        )

    def _confirm_short(
        self,
        latest: Candle,
        previous: Candle,
        pullback_ema: float,
        upper_wick: float,
        body_size: float,
        confirm_body_ok: bool,
    ) -> bool:
        if self.confirmation_mode == "Fast":
            return latest.close < pullback_ema and latest.close < latest.open
        if self.confirmation_mode == "Strict":
            return latest.close < pullback_ema and latest.close < previous.low and latest.close < latest.open and confirm_body_ok
        return latest.close < pullback_ema and latest.close < latest.open and (
            latest.close < previous.low or upper_wick >= body_size * 0.50
        )

    def _quality_score(
        self,
        is_long: bool,
        bull_trend: bool,
        bear_trend: bool,
        body_size: float,
        atr_value: float,
        close: float,
        pullback_ema: float,
        lower_wick: float,
        upper_wick: float,
        mcginley_distance: float,
        rsi_value: float,
    ) -> int:
        trend_score = 25 if (is_long and bull_trend) or ((not is_long) and bear_trend) else 0
        body_ratio = body_size / atr_value if atr_value else 0.0
        if body_ratio >= 0.75:
            body_score = 20
        elif body_ratio >= 0.50:
            body_score = 16
        elif body_ratio >= 0.35:
            body_score = 12
        elif body_ratio >= 0.20:
            body_score = 8
        else:
            body_score = 4

        distance_ratio = abs(close - pullback_ema) / atr_value if atr_value else 10.0
        if distance_ratio <= 0.25:
            distance_score = 20
        elif distance_ratio <= 0.50:
            distance_score = 16
        elif distance_ratio <= 0.75:
            distance_score = 12
        elif distance_ratio <= 1.00:
            distance_score = 8
        else:
            distance_score = 4

        relevant_wick = lower_wick if is_long else upper_wick
        if relevant_wick >= body_size * 0.50:
            wick_score = 10
        elif relevant_wick >= body_size * 0.25:
            wick_score = 6
        else:
            wick_score = 3

        mcginley_score = 10
        if self.use_mcginley_filter:
            threshold = atr_value * self.mcginley_distance_atr
            mcginley_score = 10 if mcginley_distance >= threshold * 1.5 else 7 if mcginley_distance >= threshold else 3

        rsi_score = 10
        if self.use_rsi_filter:
            if is_long:
                rsi_score = 10 if rsi_value >= self.rsi_long_min + 5 else 7 if rsi_value >= self.rsi_long_min else 3
            else:
                rsi_score = 10 if rsi_value <= self.rsi_short_max - 5 else 7 if rsi_value <= self.rsi_short_max else 3

        return min(100, trend_score + body_score + distance_score + wick_score + mcginley_score + rsi_score + 5)

    def _mcginley(self, values: List[float], length: int) -> Optional[float]:
        if not values or len(values) < length:
            return None
        seed_values = ema_series(values, length)
        line = seed_values[0]
        for close in values[1:]:
            ratio = close / line if line else 1.0
            divider = length * (ratio ** 4)
            line = line + (close - line) / divider if divider else close
        return line

    async def _manage_position(self, position: OpenPosition, data: MarketDataEvent) -> None:
        candle = data.candle
        tp1 = float(position.metadata.get("tp1", position.target_price))
        tp2 = float(position.metadata.get("tp2", position.target_price))
        tp3 = float(position.metadata.get("tp3", position.target_price))
        if position.side.upper() == "BUY":
            if candle.high >= tp3:
                await self._close_position(position, candle, tp3, "tp3")
                return
            if candle.high >= tp2:
                position.best_stage = max(position.best_stage, 2)
            elif candle.high >= tp1:
                position.best_stage = max(position.best_stage, 1)
            if candle.low <= position.stop_loss:
                protected_price = tp2 if position.best_stage == 2 else tp1 if position.best_stage == 1 else position.stop_loss
                reason = "protected_tp" if position.best_stage else "stop"
                await self._close_position(position, candle, protected_price, reason)
        else:
            if candle.low <= tp3:
                await self._close_position(position, candle, tp3, "tp3")
                return
            if candle.low <= tp2:
                position.best_stage = max(position.best_stage, 2)
            elif candle.low <= tp1:
                position.best_stage = max(position.best_stage, 1)
            if candle.high >= position.stop_loss:
                protected_price = tp2 if position.best_stage == 2 else tp1 if position.best_stage == 1 else position.stop_loss
                reason = "protected_tp" if position.best_stage else "stop"
                await self._close_position(position, candle, protected_price, reason)

    async def _close_position(
        self,
        position: OpenPosition,
        candle: Candle,
        exit_price: float,
        reason: str,
    ) -> None:
        trade = build_trade_record(self.name, position, candle.close_time, exit_price, reason=reason)
        self.record_trade(trade)
        self.logger.info(
            "EXIT_TRIGGER symbol=%s reason=%s exit=%.4f pnl=%.4f best_stage=%s",
            position.symbol,
            reason,
            exit_price,
            trade.pnl,
            position.best_stage,
        )
        self.positions.pop(position.symbol, None)
