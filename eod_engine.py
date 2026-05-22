from dataclasses import dataclass

import config
from market_context import (
    SENTIMENT_EXTREMELY_BEARISH,
    SENTIMENT_EXTREMELY_BULLISH,
    SENTIMENT_MILDLY_BEARISH,
    SENTIMENT_MILDLY_BULLISH,
    SENTIMENT_NEUTRAL,
)


ACTION_SQUARE_OFF = "EOD_SQUARE_OFF"
ACTION_CARRY = "EOD_CARRY"
ACTION_SLICE_CALL_SIDE = "EOD_SLICE_CALL_SIDE"
ACTION_SLICE_PUT_SIDE = "EOD_SLICE_PUT_SIDE"
ACTION_RECENTER_CONDOR = "BTST_RECENTER"


@dataclass
class EodDecision:
    action: str
    reason: str
    metrics: dict

    def as_dict(self):
        return {
            "action": self.action,
            "reason": self.reason,
            "metrics": self.metrics,
        }


def _normalize_order_sequence(state):
    execution_info = state.get("execution_info", {}) if state else {}
    sequence = []
    for item in execution_info.get("order_sequence", []):
        if len(item) == 2:
            sequence.append((str(item[0]), str(item[1]).upper()))
    if not sequence:
        for leg_name in (state or {}).get("legs", {}):
            sequence.append((leg_name, "SELL" if leg_name.startswith("sell") else "BUY"))
    return sequence


def _net_from_order_sequence(prices, order_sequence):
    net = 0.0
    for leg_name, transaction_type in order_sequence:
        price = float(prices.get(leg_name, 0.0) or 0.0)
        if transaction_type == "SELL":
            net += price
        elif transaction_type == "BUY":
            net -= price
    return net


def _pnl_from_order_sequence(entry, exits, qty, order_sequence):
    pnl = 0.0
    for leg_name, transaction_type in order_sequence:
        entry_price = float(entry.get(leg_name, 0.0) or 0.0)
        exit_price = float(exits.get(leg_name, 0.0) or 0.0)
        if transaction_type == "SELL":
            pnl += (entry_price - exit_price) * qty
        elif transaction_type == "BUY":
            pnl += (exit_price - entry_price) * qty
    return pnl


def _spread_width(strikes):
    values = [float(value) for key, value in (strikes or {}).items() if key != "atm" and value not in (None, "")]
    return max(values) - min(values) if len(values) >= 2 else 0.0


def _direction_for_strategy(strategy_type):
    strategy_type = str(strategy_type or "").upper()
    if "BULL" in strategy_type:
        return "BULLISH"
    if "BEAR" in strategy_type:
        return "BEARISH"
    return ""


def _is_directional(strategy_type):
    strategy_type = str(strategy_type or "").upper()
    return "BULL" in strategy_type or "BEAR" in strategy_type or "SPREAD" in strategy_type


def _is_neutral(strategy_type):
    return str(strategy_type or "").upper() in ("IRON_CONDOR", "IRON_BUTTERFLY")


def _is_aligned(strategy_type, sentiment):
    direction = _direction_for_strategy(strategy_type)
    if direction == "BULLISH":
        return sentiment in (SENTIMENT_EXTREMELY_BULLISH, SENTIMENT_MILDLY_BULLISH)
    if direction == "BEARISH":
        return sentiment in (SENTIMENT_EXTREMELY_BEARISH, SENTIMENT_MILDLY_BEARISH)
    return False


def _is_bullish_shift(sentiment):
    return sentiment in (SENTIMENT_EXTREMELY_BULLISH, SENTIMENT_MILDLY_BULLISH)


def _is_bearish_shift(sentiment):
    return sentiment in (SENTIMENT_EXTREMELY_BEARISH, SENTIMENT_MILDLY_BEARISH)


def _directional_metrics(state, current_prices):
    qty = int(state.get("quantity", 0) or 0)
    entry = state.get("entry_prices", {})
    sequence = _normalize_order_sequence(state)
    entry_net = float(state.get("entry_net_premium") or _net_from_order_sequence(entry, sequence))
    live_net = _net_from_order_sequence(current_prices, sequence)
    pnl = _pnl_from_order_sequence(entry, current_prices, qty, sequence)
    capital_deployed = float(
        state.get("capital_deployed")
        or state.get("sizing", {}).get("capital_deployed")
        or state.get("execution_info", {}).get("defined_loss_rupees")
        or abs(entry_net * qty)
        or 0.0
    )
    roc_pct = (pnl / capital_deployed * 100.0) if capital_deployed > 0 else 0.0

    max_profit = float(state.get("max_profit_rupees") or 0.0)
    if max_profit <= 0:
        width = _spread_width(state.get("strikes", {}))
        if entry_net >= 0:
            max_profit = max(0.0, entry_net * qty)
        else:
            max_profit = max(0.0, (width + entry_net) * qty)

    max_profit_capture_pct = (pnl / max_profit * 100.0) if max_profit > 0 else 0.0
    return {
        "entry_net": round(entry_net, 4),
        "live_net": round(live_net, 4),
        "pnl": round(pnl, 2),
        "capital_deployed": round(capital_deployed, 2),
        "roc_pct": round(roc_pct, 4),
        "max_profit_rupees": round(max_profit, 2),
        "max_profit_capture_pct": round(max_profit_capture_pct, 4),
    }


def evaluate_eod_decision(state, market_context, current_prices):
    strategy_type = state.get("strategy_type") or state.get("execution_info", {}).get("strategy_type", "")
    sentiment = getattr(market_context, "sentiment", None)
    metrics = {
        "strategy_type": strategy_type,
        "sentiment": sentiment,
        "flow_signal": getattr(market_context, "flow_signal", None),
        "straddle_signal": getattr(market_context, "straddle_signal", None),
        "pcr": getattr(market_context, "pcr", None),
        "dte": getattr(market_context, "dte", None),
    }

    if not market_context or sentiment in (None, "UNKNOWN"):
        return EodDecision(ACTION_SQUARE_OFF, "EOD_CONTEXT_UNAVAILABLE", metrics)

    if _is_neutral(strategy_type):
        if sentiment == SENTIMENT_NEUTRAL:
            if str(strategy_type or "").upper() == "IRON_BUTTERFLY":
                return EodDecision(ACTION_RECENTER_CONDOR, "NEUTRAL_BTST_PREFERS_IRON_CONDOR", metrics)
            return EodDecision(ACTION_CARRY, "NEUTRAL_STILL_ALIGNED", metrics)
        if _is_bullish_shift(sentiment):
            return EodDecision(ACTION_SLICE_CALL_SIDE, "BULLISH_REGIME_SHIFT", metrics)
        if _is_bearish_shift(sentiment):
            return EodDecision(ACTION_SLICE_PUT_SIDE, "BEARISH_REGIME_SHIFT", metrics)
        return EodDecision(ACTION_SQUARE_OFF, "NEUTRAL_REGIME_UNKNOWN", metrics)

    if _is_directional(strategy_type):
        directional_metrics = _directional_metrics(state, current_prices)
        metrics.update(directional_metrics)
        if not _is_aligned(strategy_type, sentiment):
            return EodDecision(ACTION_SQUARE_OFF, "DIRECTIONAL_COUNTER_TREND", metrics)

        strategy_params = state.get("strategy_params", {}) or {}
        roc_exit = float(strategy_params.get("btst_auto_exit_roc_pct", config.DIRECTIONAL_BTST_AUTO_EXIT_ROC_PCT))
        profit_exit = 70.0
        if directional_metrics["roc_pct"] > roc_exit:
            return EodDecision(ACTION_SQUARE_OFF, "DIRECTIONAL_ROC_LOCK", metrics)
        if directional_metrics["max_profit_capture_pct"] > profit_exit:
            return EodDecision(ACTION_SQUARE_OFF, "DIRECTIONAL_MAX_PROFIT_LOCK", metrics)
        return EodDecision(ACTION_CARRY, "DIRECTIONAL_BTST_CARRY", metrics)

    return EodDecision(ACTION_SQUARE_OFF, "UNSUPPORTED_EOD_STRATEGY", metrics)
