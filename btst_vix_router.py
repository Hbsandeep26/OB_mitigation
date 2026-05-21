import datetime
import logging
from dataclasses import dataclass, field

import config
from market_context import (
    FLOW_SIGNAL_BEARISH,
    FLOW_SIGNAL_BULLISH,
    FLOW_SIGNAL_NEUTRAL,
    STRADDLE_CONTRACTING,
    STRADDLE_EXPANDING,
    SENTIMENT_EXTREMELY_BEARISH,
    SENTIMENT_EXTREMELY_BULLISH,
    SENTIMENT_MILDLY_BEARISH,
    SENTIMENT_MILDLY_BULLISH,
    SENTIMENT_NEUTRAL,
    build_oi_flow_context,
    build_market_context,
)


STRATEGY_IRON_BUTTERFLY = "IRON_BUTTERFLY"
STRATEGY_IRON_CONDOR = "IRON_CONDOR"
STRATEGY_BTST_BULL_PUT_CREDIT = "BTST_BULL_PUT_CREDIT"
STRATEGY_BTST_BULL_CALL_DEBIT = "BTST_BULL_CALL_DEBIT"
STRATEGY_BTST_BEAR_CALL_CREDIT = "BTST_BEAR_CALL_CREDIT"
STRATEGY_BTST_BEAR_PUT_DEBIT = "BTST_BEAR_PUT_DEBIT"

SIGNAL_BULLISH = "BULLISH"
SIGNAL_BEARISH = "BEARISH"
SIGNAL_NEUTRAL = "NEUTRAL"
SIGNAL_NO_TRADE = "NO_TRADE"


@dataclass
class StrategyRoute:
    strategy_type: str = ""
    legs: dict = field(default_factory=dict)
    entry_prices: dict = field(default_factory=dict)
    strikes: dict = field(default_factory=dict)
    drift_threshold: float = 0.0
    metadata: dict = field(default_factory=dict)
    order_sequence: list = field(default_factory=list)
    carry_overnight: bool = False
    no_trade_reason: str = ""


@dataclass
class BtstMomentumSignal:
    signal: str
    daily_range_close: float
    reason: str = ""


def is_btst_strategy(strategy_type):
    return str(strategy_type or "").startswith("BTST_")


def drift_threshold_for_strategy(strategy_type):
    if strategy_type == STRATEGY_IRON_CONDOR:
        return config.CONDOR_ATM_DRIFT_THRESHOLD
    return config.ATM_DRIFT_EJECT_THRESHOLD


def is_btst_momentum_time(now=None):
    now = now or datetime.datetime.now()
    return now.hour == 15 and now.minute == 25


def _strike_value(strike):
    try:
        return float(strike)
    except (TypeError, ValueError):
        return None


def _sorted_strikes(option_chain_data):
    strikes = []
    for strike_data in option_chain_data:
        strike = _strike_value(strike_data.get("strike_price"))
        if strike is not None:
            strikes.append(strike)
    return sorted(set(strikes))


def _nearest_chain_strike(option_chain_data, target_price):
    strikes = _sorted_strikes(option_chain_data)
    if not strikes:
        return None
    return min(strikes, key=lambda strike: abs(strike - float(target_price)))


def _nearest_otm_strike(option_chain_data, reference_strike, direction):
    strikes = _sorted_strikes(option_chain_data)
    reference = float(reference_strike)
    if direction == "above":
        candidates = [strike for strike in strikes if strike > reference]
    else:
        candidates = [strike for strike in strikes if strike < reference]
    if not candidates:
        return None
    return min(candidates, key=lambda strike: abs(strike - reference))


def _next_farther_otm_strike(option_chain_data, short_strike, direction):
    strikes = _sorted_strikes(option_chain_data)
    short = float(short_strike)
    if direction == "above":
        candidates = [strike for strike in strikes if strike > short]
    else:
        candidates = [strike for strike in strikes if strike < short]
    if not candidates:
        return None
    return min(candidates, key=lambda strike: abs(strike - short))


def _farther_otm_strike_candidates(option_chain_data, short_strike, direction, min_steps=2, max_steps=3):
    strikes = _sorted_strikes(option_chain_data)
    short = float(short_strike)
    if short not in strikes:
        return []
    short_idx = strikes.index(short)
    candidates = []
    for offset in range(int(min_steps), int(max_steps) + 1):
        target_idx = short_idx + offset if direction == "above" else short_idx - offset
        if 0 <= target_idx < len(strikes):
            candidates.append(strikes[target_idx])
    return candidates


def _option_info_at_strike(option_chain_data, strike, option_type):
    target = _strike_value(strike)
    if target is None:
        return None
    key = "call_options" if option_type == "call" else "put_options"
    for strike_data in option_chain_data:
        current = _strike_value(strike_data.get("strike_price"))
        if current == target:
            return strike_data.get(key, {}) or None
    return None


def _ltp(option_info):
    try:
        return float(option_info.get("market_data", {}).get("ltp", 0.0) or 0.0)
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _instrument_key(option_info):
    if not option_info:
        return ""
    return option_info.get("instrument_key", "")


def _select_long_wing_by_premium(option_chain_data, option_type, short_strike, target_buy_premium, direction):
    best_diff = float("inf")
    selected_key = ""
    selected_ltp = 0.0
    selected_strike = 0.0
    short = float(short_strike)

    for strike_data in option_chain_data:
        strike = _strike_value(strike_data.get("strike_price"))
        if strike is None:
            continue
        if direction == "above" and strike <= short:
            continue
        if direction == "below" and strike >= short:
            continue

        option_info = strike_data.get("call_options" if option_type == "call" else "put_options", {}) or {}
        option_ltp = _ltp(option_info)
        if option_ltp <= 0:
            continue

        diff = abs(option_ltp - target_buy_premium)
        if diff < best_diff:
            best_diff = diff
            selected_key = option_info.get("instrument_key", "")
            selected_ltp = option_ltp
            selected_strike = strike

    return selected_key, selected_ltp, selected_strike


def calculate_iron_condor_legs(index_symbol, spot_price, option_chain_data, buy_leg_percent=None):
    buy_leg_percent = config.BUY_LEG_PERCENT if buy_leg_percent is None else buy_leg_percent
    atm_strike = _nearest_chain_strike(option_chain_data, spot_price)
    if atm_strike is None:
        logging.warning("Option chain has no strikes. Cannot calculate Iron Condor.")
        return None, None, None

    sell_ce_strike = _nearest_chain_strike(option_chain_data, atm_strike + config.CONDOR_SHORT_STRIKE_OFFSET)
    sell_pe_strike = _nearest_chain_strike(option_chain_data, atm_strike - config.CONDOR_SHORT_STRIKE_OFFSET)
    if sell_ce_strike is None or sell_pe_strike is None:
        logging.warning("Option chain is missing Condor short strikes.")
        return None, None, None
    if sell_ce_strike <= atm_strike or sell_pe_strike >= atm_strike:
        logging.warning(
            "Condor short strikes inverted around ATM: atm=%s, sell_ce=%s, sell_pe=%s.",
            atm_strike,
            sell_ce_strike,
            sell_pe_strike,
        )
        return None, None, None

    sell_ce_info = _option_info_at_strike(option_chain_data, sell_ce_strike, "call")
    sell_pe_info = _option_info_at_strike(option_chain_data, sell_pe_strike, "put")
    sell_ce_key = _instrument_key(sell_ce_info)
    sell_pe_key = _instrument_key(sell_pe_info)
    sell_ce_ltp = _ltp(sell_ce_info)
    sell_pe_ltp = _ltp(sell_pe_info)
    if not sell_ce_key or not sell_pe_key or sell_ce_ltp <= 0 or sell_pe_ltp <= 0:
        logging.warning("Condor short leg data missing or stale.")
        return None, None, None

    target_ce_buy = sell_ce_ltp * (buy_leg_percent / 100.0)
    target_pe_buy = sell_pe_ltp * (buy_leg_percent / 100.0)

    buy_ce_key, buy_ce_ltp, buy_ce_strike = _select_long_wing_by_premium(
        option_chain_data, "call", sell_ce_strike, target_ce_buy, "above"
    )
    buy_pe_key, buy_pe_ltp, buy_pe_strike = _select_long_wing_by_premium(
        option_chain_data, "put", sell_pe_strike, target_pe_buy, "below"
    )

    if not buy_ce_key or not buy_pe_key:
        logging.warning("Option chain is missing Condor protective wings.")
        return None, None, None
    if buy_ce_strike <= sell_ce_strike or buy_pe_strike >= sell_pe_strike:
        logging.warning(
            "Condor wing overlap/inversion rejected: sell_ce=%s, buy_ce=%s, sell_pe=%s, buy_pe=%s.",
            sell_ce_strike,
            buy_ce_strike,
            sell_pe_strike,
            buy_pe_strike,
        )
        return None, None, None

    legs = {
        "sell_ce": sell_ce_key,
        "sell_pe": sell_pe_key,
        "buy_ce": buy_ce_key,
        "buy_pe": buy_pe_key,
    }
    prices = {
        "sell_ce": sell_ce_ltp,
        "sell_pe": sell_pe_ltp,
        "buy_ce": buy_ce_ltp,
        "buy_pe": buy_pe_ltp,
    }
    strikes = {
        "sell_ce": sell_ce_strike,
        "sell_pe": sell_pe_strike,
        "buy_ce": buy_ce_strike,
        "buy_pe": buy_pe_strike,
        "atm": atm_strike,
    }

    logging.info("Selected Iron Condor legs: %s", legs)
    logging.info("Selected Iron Condor prices: %s", prices)
    logging.info("Selected Iron Condor strikes: %s", strikes)
    return legs, prices, strikes


def route_intraday_neutral_strategy(index_symbol, spot_price, option_chain_data, india_vix, butterfly_calculator):
    if india_vix is None:
        return StrategyRoute(no_trade_reason="India VIX unavailable")

    live_vix = float(india_vix)
    if live_vix < config.IRON_BUTTERFLY_VIX_ACTIVATION:
        legs, entry_prices, strikes = butterfly_calculator(
            index_symbol,
            spot_price,
            option_chain_data,
            buy_leg_percent=config.BUY_LEG_PERCENT,
        )
        strategy_type = STRATEGY_IRON_BUTTERFLY
    else:
        legs, entry_prices, strikes = calculate_iron_condor_legs(
            index_symbol,
            spot_price,
            option_chain_data,
            buy_leg_percent=config.BUY_LEG_PERCENT,
        )
        strategy_type = STRATEGY_IRON_CONDOR

    if not legs:
        return StrategyRoute(
            strategy_type=strategy_type,
            drift_threshold=drift_threshold_for_strategy(strategy_type),
            metadata={"india_vix": live_vix},
            no_trade_reason=f"{strategy_type} leg calculation failed",
        )

    return StrategyRoute(
        strategy_type=strategy_type,
        legs=legs,
        entry_prices=entry_prices,
        strikes=strikes,
        drift_threshold=drift_threshold_for_strategy(strategy_type),
        metadata={
            "india_vix": live_vix,
            "vix_toggle_level": config.INDIA_VIX_TOGGLE_LEVEL,
        },
    )


def strategy_type_for_context(market_context):
    sentiment = getattr(market_context, "sentiment", "")
    vix = getattr(market_context, "vix", None)

    if sentiment == SENTIMENT_EXTREMELY_BULLISH:
        return STRATEGY_BTST_BULL_PUT_CREDIT
    if sentiment == SENTIMENT_MILDLY_BULLISH:
        return STRATEGY_BTST_BULL_PUT_CREDIT
    if sentiment == SENTIMENT_NEUTRAL:
        if vix is None:
            return ""
        return STRATEGY_IRON_CONDOR if float(vix) >= config.IRON_CONDOR_VIX_ACTIVATION else STRATEGY_IRON_BUTTERFLY
    if sentiment == SENTIMENT_MILDLY_BEARISH:
        return STRATEGY_BTST_BEAR_CALL_CREDIT
    if sentiment == SENTIMENT_EXTREMELY_BEARISH:
        return STRATEGY_BTST_BEAR_CALL_CREDIT
    return ""


def calculate_matrix_spread_legs(index_symbol, spot_price, option_chain_data, strategy_type):
    atm_strike = _nearest_chain_strike(option_chain_data, spot_price)
    if atm_strike is None:
        return StrategyRoute(strategy_type=strategy_type, no_trade_reason="Option chain has no strikes")

    if strategy_type == STRATEGY_BTST_BULL_PUT_CREDIT:
        sell_pe_strike = _nearest_otm_strike(option_chain_data, atm_strike, "below")
        if sell_pe_strike is None:
            return StrategyRoute(strategy_type=strategy_type, no_trade_reason="Bull Put requires 2-3 strike credit width")
        sell_key, sell_price = _spread_leg(option_chain_data, sell_pe_strike, "put")
        buy_pe_strike, buy_key, buy_price = None, "", 0.0
        for candidate in _farther_otm_strike_candidates(option_chain_data, sell_pe_strike, "below", min_steps=2, max_steps=3):
            candidate_key, candidate_price = _spread_leg(option_chain_data, candidate, "put")
            if candidate_key and candidate_price > 0 and sell_price > candidate_price:
                buy_pe_strike, buy_key, buy_price = candidate, candidate_key, candidate_price
                break
        if buy_pe_strike is None or not (buy_pe_strike < sell_pe_strike < atm_strike):
            return StrategyRoute(strategy_type=strategy_type, no_trade_reason="Bull Put requires 2-3 strike credit width")
        return _spread_route(
            strategy_type,
            {"buy_pe": buy_key, "sell_pe": sell_key},
            {"buy_pe": buy_price, "sell_pe": sell_price},
            {"buy_pe": buy_pe_strike, "sell_pe": sell_pe_strike, "atm": atm_strike},
            [("buy_pe", "BUY"), ("sell_pe", "SELL")],
            SIGNAL_BULLISH,
            None,
            0.0,
        )

    if strategy_type == STRATEGY_BTST_BULL_CALL_DEBIT:
        sell_ce_strike = _nearest_otm_strike(option_chain_data, atm_strike, "above")
        if sell_ce_strike is None or sell_ce_strike <= atm_strike:
            return StrategyRoute(strategy_type=strategy_type, no_trade_reason="Bull Call OTM short strike invalid")
        buy_key, buy_price = _spread_leg(option_chain_data, atm_strike, "call")
        sell_key, sell_price = _spread_leg(option_chain_data, sell_ce_strike, "call")
        return _spread_route(
            strategy_type,
            {"buy_ce": buy_key, "sell_ce": sell_key},
            {"buy_ce": buy_price, "sell_ce": sell_price},
            {"buy_ce": atm_strike, "sell_ce": sell_ce_strike},
            [("buy_ce", "BUY"), ("sell_ce", "SELL")],
            SIGNAL_BULLISH,
            None,
            0.0,
        )

    if strategy_type == STRATEGY_BTST_BEAR_PUT_DEBIT:
        sell_pe_strike = _nearest_otm_strike(option_chain_data, atm_strike, "below")
        if sell_pe_strike is None or sell_pe_strike >= atm_strike:
            return StrategyRoute(strategy_type=strategy_type, no_trade_reason="Bear Put OTM short strike invalid")
        buy_key, buy_price = _spread_leg(option_chain_data, atm_strike, "put")
        sell_key, sell_price = _spread_leg(option_chain_data, sell_pe_strike, "put")
        return _spread_route(
            strategy_type,
            {"buy_pe": buy_key, "sell_pe": sell_key},
            {"buy_pe": buy_price, "sell_pe": sell_price},
            {"buy_pe": atm_strike, "sell_pe": sell_pe_strike},
            [("buy_pe", "BUY"), ("sell_pe", "SELL")],
            SIGNAL_BEARISH,
            None,
            0.0,
        )

    if strategy_type == STRATEGY_BTST_BEAR_CALL_CREDIT:
        sell_ce_strike = _nearest_otm_strike(option_chain_data, atm_strike, "above")
        if sell_ce_strike is None:
            return StrategyRoute(strategy_type=strategy_type, no_trade_reason="Bear Call requires 2-3 strike credit width")
        sell_key, sell_price = _spread_leg(option_chain_data, sell_ce_strike, "call")
        buy_ce_strike, buy_key, buy_price = None, "", 0.0
        for candidate in _farther_otm_strike_candidates(option_chain_data, sell_ce_strike, "above", min_steps=2, max_steps=3):
            candidate_key, candidate_price = _spread_leg(option_chain_data, candidate, "call")
            if candidate_key and candidate_price > 0 and sell_price > candidate_price:
                buy_ce_strike, buy_key, buy_price = candidate, candidate_key, candidate_price
                break
        if buy_ce_strike is None or not (atm_strike < sell_ce_strike < buy_ce_strike):
            return StrategyRoute(strategy_type=strategy_type, no_trade_reason="Bear Call requires 2-3 strike credit width")
        return _spread_route(
            strategy_type,
            {"buy_ce": buy_key, "sell_ce": sell_key},
            {"buy_ce": buy_price, "sell_ce": sell_price},
            {"buy_ce": buy_ce_strike, "sell_ce": sell_ce_strike, "atm": atm_strike},
            [("buy_ce", "BUY"), ("sell_ce", "SELL")],
            SIGNAL_BEARISH,
            None,
            0.0,
        )

    return StrategyRoute(strategy_type=strategy_type, no_trade_reason="Unsupported matrix strategy")


def route_command_center_strategy(
    index_symbol,
    expiry_date,
    spot_price,
    option_chain_data,
    india_vix,
    butterfly_calculator,
    previous_snapshot=None,
    now=None,
):
    flow_context = build_oi_flow_context(
        index_symbol,
        expiry_date,
        option_chain_data,
        spot=spot_price,
        previous_snapshot=previous_snapshot,
        now=now,
    )
    context_metadata = flow_context.as_dict()
    base_metadata = {
        "market_context": context_metadata,
        "entry_regime_signal": flow_context.flow_signal,
        "straddle_signal": flow_context.straddle_signal,
        "oi_flow_snapshot": flow_context.oi_flow_snapshot or {},
    }

    strategy_type = ""
    if flow_context.flow_signal == FLOW_SIGNAL_NEUTRAL and flow_context.straddle_signal == STRADDLE_CONTRACTING:
        neutral_route = route_intraday_neutral_strategy(
            index_symbol,
            spot_price,
            option_chain_data,
            india_vix,
            butterfly_calculator,
        )
        neutral_route.metadata = {**(neutral_route.metadata or {}), **base_metadata}
        return neutral_route

    if flow_context.flow_signal == FLOW_SIGNAL_BULLISH and flow_context.straddle_signal == STRADDLE_EXPANDING:
        strategy_type = STRATEGY_BTST_BULL_PUT_CREDIT
    elif flow_context.flow_signal == FLOW_SIGNAL_BEARISH and flow_context.straddle_signal == STRADDLE_EXPANDING:
        strategy_type = STRATEGY_BTST_BEAR_CALL_CREDIT
    else:
        reason = flow_context.no_trade_reason or (
            f"Flow {flow_context.flow_signal} with straddle {flow_context.straddle_signal} is not tradable"
        )
        return StrategyRoute(metadata=base_metadata, no_trade_reason=reason)

    route = calculate_matrix_spread_legs(index_symbol, spot_price, option_chain_data, strategy_type)
    route.metadata = {**(route.metadata or {}), **base_metadata}
    if not route.legs and not route.no_trade_reason:
        route.no_trade_reason = f"{strategy_type} leg calculation failed"
    return route


def route_pcr_dte_strategy(index_symbol, expiry_date, spot_price, option_chain_data, india_vix, butterfly_calculator, now=None):
    market_context = build_market_context(
        index_symbol,
        expiry_date,
        option_chain_data,
        india_vix,
        now=now,
        spot=spot_price,
    )
    strategy_type = strategy_type_for_context(market_context)
    context_metadata = market_context.as_dict()

    if not strategy_type:
        return StrategyRoute(
            metadata={"market_context": context_metadata},
            no_trade_reason="Market context did not produce a tradable strategy",
        )

    if strategy_type in (STRATEGY_IRON_BUTTERFLY, STRATEGY_IRON_CONDOR):
        route = route_intraday_neutral_strategy(
            index_symbol,
            spot_price,
            option_chain_data,
            india_vix,
            butterfly_calculator,
        )
    else:
        route = calculate_matrix_spread_legs(index_symbol, spot_price, option_chain_data, strategy_type)

    route.strategy_type = route.strategy_type or strategy_type
    route.metadata = {**(route.metadata or {}), "market_context": context_metadata}
    if not route.legs and not route.no_trade_reason:
        route.no_trade_reason = f"{route.strategy_type} leg calculation failed"
    return route


def calculate_ema(values, period=20):
    clean_values = [float(value) for value in values if value is not None]
    if len(clean_values) < period:
        return None

    ema_value = sum(clean_values[:period]) / period
    multiplier = 2.0 / (period + 1.0)
    for value in clean_values[period:]:
        ema_value = (value - ema_value) * multiplier + ema_value
    return ema_value


def candles_market_profile(current_price, candles):
    ordered = sorted(candles or [], key=lambda candle: str(candle[0]))
    highs = []
    lows = []
    closes = []
    for candle in ordered:
        try:
            highs.append(float(candle[2]))
            lows.append(float(candle[3]))
            closes.append(float(candle[4]))
        except (TypeError, ValueError, IndexError):
            continue

    if not highs or not lows or not closes:
        return None

    current = float(current_price)
    return {
        "current_price": current,
        "daily_high": max(max(highs), current),
        "daily_low": min(min(lows), current),
        "ema_15m_20": calculate_ema(closes, period=20),
        "candle_count": len(closes),
    }


def evaluate_btst_momentum_signal(current_price, ema_15m_20, daily_low, daily_high):
    if ema_15m_20 is None:
        return BtstMomentumSignal(SIGNAL_NO_TRADE, 0.0, "15-minute 20 EMA unavailable")
    if daily_high <= daily_low:
        return BtstMomentumSignal(SIGNAL_NO_TRADE, 0.0, "Invalid daily range")

    current = float(current_price)
    daily_range_close = (current - float(daily_low)) / (float(daily_high) - float(daily_low))

    if current > float(ema_15m_20) and daily_range_close >= config.BTST_BULLISH_RANGE_CLOSE:
        return BtstMomentumSignal(SIGNAL_BULLISH, daily_range_close, "Bullish momentum matrix")

    if current < float(ema_15m_20) and daily_range_close <= config.BTST_BEARISH_RANGE_CLOSE:
        return BtstMomentumSignal(SIGNAL_BEARISH, daily_range_close, "Bearish momentum matrix")

    if config.BTST_BEARISH_RANGE_CLOSE < daily_range_close < config.BTST_NEUTRAL_RANGE_CLOSE_UPPER:
        return BtstMomentumSignal(SIGNAL_NEUTRAL, daily_range_close, "Balanced daily range close")

    return BtstMomentumSignal(SIGNAL_NO_TRADE, daily_range_close, "Momentum matrix not aligned")


def _spread_leg(option_chain_data, strike, option_type):
    info = _option_info_at_strike(option_chain_data, strike, option_type)
    key = _instrument_key(info)
    price = _ltp(info)
    return key, price


def _spread_route(strategy_type, legs, prices, strikes, order_sequence, signal, india_vix, daily_range_close):
    if not legs or any(not token for token in legs.values()) or any(price <= 0 for price in prices.values()):
        return StrategyRoute(
            strategy_type=strategy_type,
            metadata={"signal": signal, "india_vix": india_vix, "daily_range_close": daily_range_close},
            no_trade_reason=f"{strategy_type} leg data missing",
        )

    if "DEBIT" in str(strategy_type).upper():
        return StrategyRoute(
            strategy_type=strategy_type,
            metadata={"signal": signal, "india_vix": india_vix, "daily_range_close": daily_range_close},
            no_trade_reason="Directional debit spreads are disabled",
        )

    net_credit = 0.0
    for leg_name, transaction_type in order_sequence:
        price = float(prices.get(leg_name, 0.0) or 0.0)
        net_credit += price if str(transaction_type).upper() == "SELL" else -price
    if "CREDIT" in str(strategy_type).upper() and net_credit <= 0:
        return StrategyRoute(
            strategy_type=strategy_type,
            metadata={"signal": signal, "india_vix": india_vix, "daily_range_close": daily_range_close},
            no_trade_reason=f"{strategy_type} is not a credit spread",
        )

    return StrategyRoute(
        strategy_type=strategy_type,
        legs=legs,
        entry_prices=prices,
        strikes=strikes,
        metadata={"signal": signal, "india_vix": india_vix, "daily_range_close": daily_range_close},
        order_sequence=order_sequence,
        carry_overnight=True,
    )


def calculate_btst_spread_legs(index_symbol, spot_price, option_chain_data, signal, india_vix, daily_range_close):
    atm_strike = _nearest_chain_strike(option_chain_data, spot_price)
    if atm_strike is None:
        return StrategyRoute(no_trade_reason="Option chain has no strikes")

    if signal == SIGNAL_BULLISH:
        route = calculate_matrix_spread_legs(index_symbol, spot_price, option_chain_data, STRATEGY_BTST_BULL_PUT_CREDIT)
        route.metadata.update({"signal": signal, "india_vix": india_vix, "daily_range_close": daily_range_close})
        return route

    if signal == SIGNAL_BEARISH:
        route = calculate_matrix_spread_legs(index_symbol, spot_price, option_chain_data, STRATEGY_BTST_BEAR_CALL_CREDIT)
        route.metadata.update({"signal": signal, "india_vix": india_vix, "daily_range_close": daily_range_close})
        return route

    return StrategyRoute(no_trade_reason="No BTST spread for neutral/no-trade signal")


def route_btst_momentum_strategy(index_symbol, spot_price, option_chain_data, india_vix, ema_15m_20, daily_low, daily_high):
    signal = evaluate_btst_momentum_signal(spot_price, ema_15m_20, daily_low, daily_high)
    if signal.signal in (SIGNAL_NEUTRAL, SIGNAL_NO_TRADE):
        return StrategyRoute(
            metadata={
                "signal": signal.signal,
                "daily_range_close": signal.daily_range_close,
                "india_vix": india_vix,
            },
            no_trade_reason=signal.reason,
        )

    if india_vix is None:
        return StrategyRoute(
            metadata={"signal": signal.signal, "daily_range_close": signal.daily_range_close},
            no_trade_reason="India VIX unavailable",
        )

    return calculate_btst_spread_legs(
        index_symbol,
        spot_price,
        option_chain_data,
        signal.signal,
        float(india_vix),
        signal.daily_range_close,
    )
