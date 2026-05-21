import datetime
import logging
import os
import time

import config
import state_manager
from btst_vix_router import drift_threshold_for_strategy, is_btst_strategy
from eod_engine import evaluate_eod_decision
from market_context import (
    FLOW_SIGNAL_BEARISH,
    FLOW_SIGNAL_BULLISH,
    FLOW_SIGNAL_NEUTRAL,
    STRADDLE_EXPANDING,
    build_oi_flow_context,
    build_market_context,
    days_to_expiry,
)
from broker import get_broker
from notifier import send_telegram_alert

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _nearest_chain_strike(option_chain_data, spot_price):
    strikes = [
        strike_data.get("strike_price")
        for strike_data in option_chain_data
        if strike_data.get("strike_price") is not None
    ]
    if not strikes:
        return None
    return min(strikes, key=lambda strike: abs(float(strike) - float(spot_price)))


def _best_effort_prices(live_data, legs, entries):
    return {
        "sell_ce": live_data.get(legs["sell_ce"], {}).get("ltp", entries["sell_ce"]),
        "sell_pe": live_data.get(legs["sell_pe"], {}).get("ltp", entries["sell_pe"]),
        "buy_ce": live_data.get(legs["buy_ce"], {}).get("ltp", entries["buy_ce"]),
        "buy_pe": live_data.get(legs["buy_pe"], {}).get("ltp", entries["buy_pe"]),
    }


def _best_effort_leg_prices(live_data, legs, entries):
    return {
        leg_name: live_data.get(token, {}).get("ltp", entries.get(leg_name, 0.0))
        for leg_name, token in legs.items()
    }


def _fresh_ltp(live_data, token, leg_name):
    tick = live_data.get(token)
    if not tick or tick.get("ltp", 0) <= 0:
        state_manager.update_state("feed_status", f"MISSING:{leg_name}")
        raise ValueError(f"Missing live price for {leg_name}")

    tick_ts = tick.get("ts")
    if tick_ts is None:
        state_manager.update_state("feed_status", f"NO_TS:{leg_name}")
        raise ValueError(f"Live price for {leg_name} has no timestamp")

    age = time.time() - float(tick_ts)
    if age > config.MAX_FEED_STALENESS_SECONDS:
        state_manager.update_state("feed_status", f"STALE:{leg_name}:{age:.1f}s")
        raise ValueError(f"Stale live price for {leg_name}: {age:.1f}s")

    return float(tick["ltp"])


def _fresh_option_snapshot(legs):
    try:
        quotes = get_broker().get_fresh_option_quotes(list(legs.values()))
    except Exception as err:
        logging.warning("Emergency option confirmation failed: %s", err)
        return None

    prices = {
        "sell_ce": float(quotes.get(legs["sell_ce"], 0.0) or 0.0),
        "sell_pe": float(quotes.get(legs["sell_pe"], 0.0) or 0.0),
        "buy_ce": float(quotes.get(legs["buy_ce"], 0.0) or 0.0),
        "buy_pe": float(quotes.get(legs["buy_pe"], 0.0) or 0.0),
    }
    return prices if all(value > 0 for value in prices.values()) else None


def _fresh_spot_snapshot(index_symbol):
    try:
        spot = get_broker().get_spot_price(index_symbol)
    except Exception as err:
        logging.warning("Emergency spot confirmation failed: %s", err)
        return None
    return float(spot or 0.0) or None


def get_vix_session_profile(live_vix):
    profile = {
        "name": "SNIPER_SHIELD",
        "buy_leg_percent": config.BUY_LEG_PERCENT,
        "target_pct": config.SNIPER_TARGET_PCT,
    }
    logging.info(
        "Sniper & Shield profile selected: VIX=%.2f, buy leg premium percent=%s, target=%s%%",
        live_vix,
        profile["buy_leg_percent"],
        profile["target_pct"],
    )
    return profile


def _normalized_delta(option_info):
    delta = option_info.get("greeks", {}).get("delta")
    if delta in (None, ""):
        return None
    delta = abs(float(delta))
    return delta * 100.0 if delta <= 1.0 else delta


def calculate_iron_butterfly_legs(index_symbol, spot_price, option_chain_data, buy_leg_percent=None, wing_delta=None):
    target_wing_delta = wing_delta if wing_delta is not None else config.SNIPER_WING_DELTA
    use_delta_wings = buy_leg_percent is None and target_wing_delta > 0
    buy_leg_percent = config.BUY_LEG_PERCENT if buy_leg_percent is None else buy_leg_percent
    if use_delta_wings:
        logging.info("Calculating Iron Butterfly strikes & prices (Wing Delta=%s)...", target_wing_delta)
    else:
        logging.info("Calculating Iron Butterfly strikes & prices (Buy Leg Premium Percent=%s%%)...", buy_leg_percent)
    atm_strike = _nearest_chain_strike(option_chain_data, spot_price)
    if atm_strike is None:
        logging.warning("Option chain has no strikes. Cannot calculate Iron Butterfly.")
        return None, None, None

    atm_ce_ltp, atm_pe_ltp = 0, 0
    sell_ce_key, sell_pe_key = "", ""

    for strike_data in option_chain_data:
        if strike_data.get("strike_price") == atm_strike:
            call_info = strike_data.get("call_options", {})
            put_info = strike_data.get("put_options", {})
            if call_info and put_info:
                sell_ce_key = call_info.get("instrument_key")
                atm_ce_ltp = call_info.get("market_data", {}).get("ltp", 0)
                sell_pe_key = put_info.get("instrument_key")
                atm_pe_ltp = put_info.get("market_data", {}).get("ltp", 0)
            break

    if not sell_ce_key or not sell_pe_key:
        return None, None, None

    target_ce_buy = atm_ce_ltp * (buy_leg_percent / 100.0)
    target_pe_buy = atm_pe_ltp * (buy_leg_percent / 100.0)
    
    best_ce_diff, best_pe_diff = float("inf"), float("inf")
    buy_ce_key, buy_pe_key = "", ""
    buy_ce_ltp, buy_pe_ltp = 0, 0
    buy_ce_strike, buy_pe_strike = 0, 0
    fallback_ce, fallback_pe = None, None

    for strike_data in option_chain_data:
        strike = strike_data.get("strike_price")
        call_info = strike_data.get("call_options", {})
        put_info = strike_data.get("put_options", {})

        if strike > atm_strike and call_info:
            ce_ltp = call_info.get("market_data", {}).get("ltp", 0)
            if ce_ltp > 0:
                fallback_ce = (call_info.get("instrument_key"), ce_ltp, strike)
            ce_delta = _normalized_delta(call_info) if use_delta_wings else None
            diff = abs(ce_delta - target_wing_delta) if ce_delta is not None else abs(ce_ltp - target_ce_buy)
            if ce_ltp > 0 and diff < best_ce_diff and (not use_delta_wings or ce_delta is not None):
                best_ce_diff = diff
                buy_ce_key = call_info.get("instrument_key")
                buy_ce_ltp = ce_ltp
                buy_ce_strike = strike

        if strike < atm_strike and put_info:
            pe_ltp = put_info.get("market_data", {}).get("ltp", 0)
            if pe_ltp > 0 and fallback_pe is None:
                fallback_pe = (put_info.get("instrument_key"), pe_ltp, strike)
            pe_delta = _normalized_delta(put_info) if use_delta_wings else None
            diff = abs(pe_delta - target_wing_delta) if pe_delta is not None else abs(pe_ltp - target_pe_buy)
            if pe_ltp > 0 and diff < best_pe_diff and (not use_delta_wings or pe_delta is not None):
                best_pe_diff = diff
                buy_pe_key = put_info.get("instrument_key")
                buy_pe_ltp = pe_ltp
                buy_pe_strike = strike

    if use_delta_wings:
        if not buy_ce_key and fallback_ce:
            buy_ce_key, buy_ce_ltp, buy_ce_strike = fallback_ce
        if not buy_pe_key and fallback_pe:
            buy_pe_key, buy_pe_ltp, buy_pe_strike = fallback_pe

    if not buy_ce_key or not buy_pe_key:
        logging.warning("Option chain is missing protective wings. Aborting calculation.")
        return None, None, None

    legs = {"sell_ce": sell_ce_key, "sell_pe": sell_pe_key, "buy_ce": buy_ce_key, "buy_pe": buy_pe_key}
    prices = {"sell_ce": atm_ce_ltp, "sell_pe": atm_pe_ltp, "buy_ce": buy_ce_ltp, "buy_pe": buy_pe_ltp}
    strikes = {"sell_ce": atm_strike, "sell_pe": atm_strike, "buy_ce": buy_ce_strike, "buy_pe": buy_pe_strike}

    logging.info("Selected Execution Legs: %s", legs)
    logging.info("Target Entry Prices: %s", prices)
    logging.info("Selected Strikes: %s", strikes)
    return legs, prices, strikes


def evaluate_btst_health(live_data, legs, entries):
    """
    Evaluates whether the current butterfly structure is healthy enough
    for overnight carry-forward. Returns (is_healthy, diagnosis_str).
    """
    try:
        live_sell_ce = _fresh_ltp(live_data, legs["sell_ce"], "sell_ce")
        live_sell_pe = _fresh_ltp(live_data, legs["sell_pe"], "sell_pe")
        live_buy_ce = _fresh_ltp(live_data, legs["buy_ce"], "buy_ce")
        live_buy_pe = _fresh_ltp(live_data, legs["buy_pe"], "buy_pe")
    except ValueError as e:
        return False, f"Missing/stale live prices: {e}"
        
    entry_net = (entries["sell_ce"] + entries["sell_pe"]) - (entries["buy_ce"] + entries["buy_pe"])
    live_net = (live_sell_ce + live_sell_pe) - (live_buy_ce + live_buy_pe)
    if live_net > entry_net:
        return False, "Position is in loss (negative PnL)"
        
    ce_spread_val = live_sell_ce - live_buy_ce
    pe_spread_val = live_sell_pe - live_buy_pe
    
    if ce_spread_val <= 0 or pe_spread_val <= 0:
        return False, "Negative spread value detected"
        
    skew_ratio = max(ce_spread_val, pe_spread_val) / min(ce_spread_val, pe_spread_val)
    if skew_ratio > config.BTST_MAX_SKEW_RATIO:
        return False, f"Skew ratio {skew_ratio:.2f} exceeds max {config.BTST_MAX_SKEW_RATIO}"
        
    ce_retention = live_sell_ce / entries["sell_ce"]
    pe_retention = live_sell_pe / entries["sell_pe"]
    
    if ce_retention < config.BTST_MIN_LEG_PCT:
        return False, f"CE leg premium retention {ce_retention:.2f} < {config.BTST_MIN_LEG_PCT}"
    if pe_retention < config.BTST_MIN_LEG_PCT:
        return False, f"PE leg premium retention {pe_retention:.2f} < {config.BTST_MIN_LEG_PCT}"
        
    return True, "Healthy"


def _entry_net(entries):
    return (entries["sell_ce"] + entries["sell_pe"]) - (entries["buy_ce"] + entries["buy_pe"])


def _live_net(prices):
    return (prices["sell_ce"] + prices["sell_pe"]) - (prices["buy_ce"] + prices["buy_pe"])


def _order_sequence_from_state(state):
    execution_info = state.get("execution_info", {}) if state else {}
    sequence = []
    for item in execution_info.get("order_sequence", []):
        if len(item) == 2:
            sequence.append((str(item[0]), str(item[1]).upper()))
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


def _max_profit_rupees(state, entry_net, order_sequence=None):
    qty = int(state.get("quantity", 0) or 0)
    configured = float(state.get("max_profit_rupees") or 0.0)
    if configured > 0:
        return configured
    if order_sequence:
        width = _spread_width(state.get("strikes", {}))
        if entry_net >= 0:
            return max(0.0, entry_net * qty)
        return max(0.0, (width + entry_net) * qty)
    return max(0.0, entry_net * qty)


def _strategy_params(state):
    return state.get("strategy_params", {}) or {}


def _target_pct_for_state(state):
    return float(
        _strategy_params(state).get(
            "target_pct",
            state.get("sniper_target_pct", config.SNIPER_TARGET_PCT),
        )
    )


def _catastrophe_multiplier_for_state(state):
    return float(
        _strategy_params(state).get(
            "catastrophe_multiplier",
            config.SNIPER_CATASTROPHE_MULTIPLIER,
        )
    )


def _positive_float(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    return value if value > 0 else 0.0


def _state_expiry_date(state):
    index_symbol = state.get("index_symbol", "NIFTY")
    return state.get("effective_expiry_date") or state.get("expiry_date") or config.get_next_expiry(index_symbol)


def _entry_signal_for_state(state):
    explicit = state.get("entry_regime_signal") or state.get("route_metadata", {}).get("entry_regime_signal")
    if explicit:
        return str(explicit).upper()
    strategy_type = str(state.get("strategy_type") or state.get("execution_info", {}).get("strategy_type", "")).upper()
    if "BULL" in strategy_type:
        return FLOW_SIGNAL_BULLISH
    if "BEAR" in strategy_type:
        return FLOW_SIGNAL_BEARISH
    if strategy_type in ("IRON_BUTTERFLY", "IRON_CONDOR"):
        return FLOW_SIGNAL_NEUTRAL
    return ""


def _is_confirmed_regime_reversal(entry_signal, flow_signal, straddle_signal):
    if straddle_signal != STRADDLE_EXPANDING:
        return False
    if entry_signal == FLOW_SIGNAL_BULLISH:
        return flow_signal == FLOW_SIGNAL_BEARISH
    if entry_signal == FLOW_SIGNAL_BEARISH:
        return flow_signal == FLOW_SIGNAL_BULLISH
    if entry_signal == FLOW_SIGNAL_NEUTRAL:
        return flow_signal in (FLOW_SIGNAL_BULLISH, FLOW_SIGNAL_BEARISH)
    return False


def _check_regime_reversal(state, current_prices, live_spot=None):
    last_check = float(state.get("last_regime_check_ts", 0.0) or 0.0)
    now_ts = time.time()
    if now_ts - last_check < float(config.FLOW_POLL_SECONDS):
        return False, {}

    index_symbol = state.get("index_symbol", "NIFTY")
    expiry_date = _state_expiry_date(state)
    try:
        chain = get_broker().get_option_chain(index_symbol, expiry_date)
        spot = live_spot or state.get("last_spot") or state.get("entry_spot") or get_broker().get_spot_price(index_symbol)
        context = build_oi_flow_context(
            index_symbol,
            expiry_date,
            chain,
            spot=spot,
            previous_snapshot=state.get("oi_flow_snapshot"),
        )
    except Exception as err:
        logging.warning("Regime reversal check skipped: %s", err)
        state_manager.update_many({
            "last_regime_check_ts": now_ts,
            "latest_regime_error": str(err),
        })
        return False, {}

    entry_signal = _entry_signal_for_state(state)
    is_reversal = _is_confirmed_regime_reversal(entry_signal, context.flow_signal, context.straddle_signal)
    reversal_count = int(state.get("regime_reversal_count", 0) or 0)
    reversal_count = reversal_count + 1 if is_reversal else 0
    state_manager.update_many({
        "last_regime_check_ts": now_ts,
        "latest_regime_signal": context.flow_signal,
        "latest_straddle_signal": context.straddle_signal,
        "regime_reversal_count": reversal_count,
        "straddle_premium": context.straddle_premium,
        "previous_straddle_premium": context.previous_straddle_premium,
        "oi_flow_snapshot": context.oi_flow_snapshot or state.get("oi_flow_snapshot", {}),
        "market_context": context.as_dict(),
        "effective_expiry_date": expiry_date,
        "dte": context.dte,
    })

    if reversal_count >= 3:
        logging.critical(
            "REGIME REVERSAL: entry=%s latest=%s/%s sustained for %s cycles.",
            entry_signal,
            context.flow_signal,
            context.straddle_signal,
            reversal_count,
        )
        return "REGIME_REVERSAL", current_prices
    return False, {}


def _dynamic_atm_drift_limit(strategy_type, live_spot, dte):
    spot = _positive_float(live_spot)
    if spot <= 0:
        return 0.0
    strategy_type = str(strategy_type or "").upper()
    near_expiry = int(dte or 0) <= 3
    if strategy_type == "IRON_BUTTERFLY":
        pct = 0.0015 if near_expiry else 0.0025
        return spot * pct
    if strategy_type == "IRON_CONDOR":
        pct = 0.0025 if near_expiry else 0.0040
        return spot * pct
    return 0.0


def _eod_context_for_state(state):
    index_symbol = state.get("index_symbol", "NIFTY")
    expiry_date = _state_expiry_date(state)
    try:
        chain = get_broker().get_option_chain(index_symbol, expiry_date)
        vix = get_broker().get_india_vix()
        spot = state.get("last_spot") or state.get("entry_spot") or get_broker().get_spot_price(index_symbol)
        flow_context = build_oi_flow_context(
            index_symbol,
            expiry_date,
            chain,
            spot=spot,
            previous_snapshot=state.get("oi_flow_snapshot"),
        )
        if flow_context.sentiment != "UNKNOWN":
            return flow_context
        return build_market_context(index_symbol, expiry_date, chain, vix, spot=spot)
    except Exception as err:
        logging.warning("EOD market context unavailable: %s", err)
        return None


def _eod_signal(state, current_prices):
    latest_state = state_manager.load_state() or state
    context = _eod_context_for_state(latest_state)
    decision = evaluate_eod_decision(latest_state, context, current_prices)
    state_manager.update_state("eod_decision", decision.as_dict())
    logging.critical("EOD decision: %s (%s)", decision.action, decision.reason)
    return decision.action, current_prices


def _btst_next_day_exit_due(state, now=None):
    now = now or datetime.datetime.now()
    created_at = state.get("created_at")
    try:
        created_date = datetime.datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S").date()
    except (TypeError, ValueError):
        return False

    try:
        exit_time = datetime.datetime.strptime(str(config.BTST_EXIT_TIME), "%H:%M").time()
    except ValueError:
        exit_time = datetime.time(9, 20)

    return created_date < now.date() and now.time() >= exit_time


def _btst_spread_risk_evaluator(live_data, legs, state):
    entries = state["entry_prices"]
    order_sequence = _order_sequence_from_state(state)
    if not order_sequence:
        return False, {}

    current_prices = {}
    for leg_name, token in legs.items():
        current_prices[leg_name] = _fresh_ltp(live_data, token, leg_name)

    entry_net = float(state.get("entry_net_premium") or _net_from_order_sequence(entries, order_sequence))
    live_net = _net_from_order_sequence(current_prices, order_sequence)
    denominator = abs(entry_net) if entry_net else 0.0
    current_profit_pct = (entry_net - live_net) / denominator if denominator > 0 else 0.0
    qty = int(state.get("quantity", 0) or 0)
    current_pnl = _pnl_from_order_sequence(entries, current_prices, qty, order_sequence)
    max_profit = _max_profit_rupees(state, entry_net, order_sequence=order_sequence)
    max_profit_capture_pct = (current_pnl / max_profit * 100.0) if max_profit > 0 else 0.0
    capital_deployed = float(
        state.get("capital_deployed")
        or state.get("sizing", {}).get("capital_deployed")
        or state.get("execution_info", {}).get("defined_loss_rupees")
        or abs(entry_net * qty)
        or 0.0
    )

    index_symbol = state.get("index_symbol", "NIFTY")
    spot_key = "NSE_INDEX|Nifty 50" if index_symbol == "NIFTY" else "BSE_INDEX|SENSEX"
    spot_tick = live_data.get(spot_key, {})
    last_spot = state.get("last_spot", state.get("entry_spot", 0))
    if spot_tick and spot_tick.get("ltp", 0) > 0:
        last_spot = float(spot_tick["ltp"])

    state_manager.update_many({
        "feed_status": "LIVE",
        "last_live_prices": current_prices,
        "live_net_premium": round(live_net, 4),
        "entry_net_premium": round(entry_net, 4),
        "current_profit_pct": round(current_profit_pct, 6),
        "current_pnl": round(current_pnl, 2),
        "max_profit_rupees": round(max_profit, 2),
        "max_profit_capture_pct": round(max_profit_capture_pct, 4),
        "last_spot": round(last_spot, 2) if last_spot else 0,
    })

    reversal_signal, reversal_prices = _check_regime_reversal(state, current_prices, live_spot=last_spot)
    if reversal_signal:
        return reversal_signal, reversal_prices

    if _btst_next_day_exit_due(state):
        logging.critical("BTST next-day exit time reached. Squaring off overnight momentum spread.")
        return "BTST_NEXT_DAY_EXIT", current_prices

    catastrophe_multiplier = _catastrophe_multiplier_for_state(state)
    catastrophe_basis = abs(entry_net * qty) or capital_deployed
    if catastrophe_basis > 0 and current_pnl <= -(catastrophe_basis * catastrophe_multiplier):
        logging.critical(
            "Directional catastrophe kill: pnl %.2f <= -%.2f.",
            current_pnl,
            catastrophe_basis * catastrophe_multiplier,
        )
        return "CATASTROPHE_KILL", current_prices

    target_pct = _target_pct_for_state(state)
    if max_profit > 0 and current_pnl >= max_profit * (target_pct / 100.0):
        logging.critical("Directional target hit at %.2f%% of max profit.", max_profit_capture_pct)
        return "SNIPER_TARGET", current_prices

    now = datetime.datetime.now()
    if now.hour > 15 or (now.hour == 15 and now.minute >= 25):
        return _eod_signal(state, current_prices)

    return False, {}


def _atm_drift_ratio(live_spot, strikes):
    atm_strike = strikes.get("atm", strikes.get("sell_ce", 0.0))
    if live_spot <= 0 or atm_strike <= 0:
        return 0.0

    ce_wing_width = abs(strikes.get("buy_ce", atm_strike) - atm_strike)
    pe_wing_width = abs(atm_strike - strikes.get("buy_pe", atm_strike))
    avg_wing_width = (ce_wing_width + pe_wing_width) / 2.0
    if avg_wing_width <= 0:
        return 0.0
    return abs(live_spot - atm_strike) / avg_wing_width


def _atm_drift_points(live_spot, strikes):
    atm_strike = strikes.get("atm", strikes.get("sell_ce", 0.0))
    if live_spot <= 0 or atm_strike <= 0:
        return 0.0
    return abs(float(live_spot) - float(atm_strike))


def risk_management_evaluator(live_data, legs):
    state = state_manager.load_state()
    if not state or "entry_prices" not in state:
        return False, {}

    entries = state["entry_prices"]
    last_live_prices = state.get("last_live_prices") or entries
    strategy_type = state.get("strategy_type", "")
    if is_btst_strategy(strategy_type):
        current_prices = _best_effort_leg_prices(live_data, legs, last_live_prices)
    else:
        current_prices = _best_effort_prices(live_data, legs, last_live_prices)

    manual_exit_file = os.path.join(BASE_DIR, "manual_exit_flag.txt")
    if os.path.exists(manual_exit_file):
        with open(manual_exit_file, "r") as f:
            manual_exit_requested = f.read().strip() == "TRUE"
        if manual_exit_requested:
            logging.critical("MANUAL EXIT TRIGGERED FROM UI. Forcing Square Off.")
            os.remove(manual_exit_file)
            send_telegram_alert(
                f"<b>MANUAL EXIT REQUESTED</b>\n{state.get('index_symbol', 'UNKNOWN')}: square off started."
            )
            return "MANUAL_EXIT", current_prices

    graceful_stop_file = os.path.join(BASE_DIR, "graceful_stop_flag.txt")
    if os.path.exists(graceful_stop_file):
        logging.critical("GRACEFUL STOP requested from UI. Forcing Square Off.")
        os.remove(graceful_stop_file)
        send_telegram_alert(
            f"<b>GRACEFUL STOP REQUESTED</b>\n{state.get('index_symbol', 'UNKNOWN')}: square off started."
            )
        return "GRACEFUL_STOP", current_prices

    if is_btst_strategy(strategy_type):
        return _btst_spread_risk_evaluator(live_data, legs, state)

    live_sell_ce = _fresh_ltp(live_data, legs["sell_ce"], "sell_ce")
    live_sell_pe = _fresh_ltp(live_data, legs["sell_pe"], "sell_pe")
    live_buy_ce = _fresh_ltp(live_data, legs["buy_ce"], "buy_ce")
    live_buy_pe = _fresh_ltp(live_data, legs["buy_pe"], "buy_pe")
    current_prices = {"sell_ce": live_sell_ce, "sell_pe": live_sell_pe, "buy_ce": live_buy_ce, "buy_pe": live_buy_pe}
    state_manager.update_state("feed_status", "LIVE")

    index_symbol = state.get("index_symbol", "NIFTY")
    spot_key = "NSE_INDEX|Nifty 50" if index_symbol == "NIFTY" else "BSE_INDEX|SENSEX"
    live_spot = _fresh_ltp(live_data, spot_key, f"{index_symbol}_spot")
    strikes = state.get("strikes", {})
    entry_net = float(state.get("entry_net_premium") or _entry_net(entries))
    live_net = _live_net(current_prices)
    current_profit_pct = (entry_net - live_net) / entry_net if entry_net > 0 else 0.0
    drift_ratio = _atm_drift_ratio(live_spot, strikes)
    drift_points = _atm_drift_points(live_spot, strikes)
    drift_threshold = drift_threshold_for_strategy(strategy_type)
    strategy_params = _strategy_params(state)
    dte = state.get("dte")
    if dte is None:
        dte = days_to_expiry(_state_expiry_date(state))
    dynamic_drift_limit = _dynamic_atm_drift_limit(strategy_type, live_spot, dte)
    drift_points_threshold = dynamic_drift_limit or _positive_float(strategy_params.get("atm_drift_points"))
    target_pct = _target_pct_for_state(state)
    catastrophe_multiplier = _catastrophe_multiplier_for_state(state)
    max_profit = _max_profit_rupees(state, entry_net)
    state_manager.update_many({
        "sniper_state": "INITIAL",
        "entry_net_premium": round(entry_net, 4),
        "sniper_targets_enabled": config.SNIPER_TARGETS_ENABLED,
        "sniper_target_pct": target_pct,
        "strategy_type": strategy_type or "IRON_BUTTERFLY",
        "atm_drift_eject_threshold": round(drift_threshold, 4),
        "atm_drift_ratio": round(drift_ratio, 3),
        "atm_drift_points": round(drift_points, 2),
        "atm_drift_points_threshold": round(drift_points_threshold, 2),
        "live_net_premium": round(live_net, 4),
        "last_live_prices": current_prices,
        "current_profit_pct": round(current_profit_pct, 6),
        "max_profit_rupees": round(max_profit, 2),
        "catastrophe_threshold": round(entry_net * catastrophe_multiplier, 4),
        "last_spot": round(live_spot, 2) if live_spot else state.get("last_spot", 0),
        "dte": int(dte or 0),
    })

    reversal_signal, reversal_prices = _check_regime_reversal(state, current_prices, live_spot=live_spot)
    if reversal_signal:
        return reversal_signal, reversal_prices

    if entry_net > 0 and live_net >= entry_net * catastrophe_multiplier:
        threshold = entry_net * catastrophe_multiplier
        if config.EMERGENCY_EXIT_CONFIRMATION_ENABLED:
            confirmed_prices = _fresh_option_snapshot(legs)
            if not confirmed_prices:
                state_manager.update_state("feed_status", "UNCONFIRMED_CATASTROPHE:NO_REST_QUOTES")
                raise ValueError("Catastrophe kill could not be confirmed with fresh broker quotes")
            confirmed_net = _live_net(confirmed_prices)
            if confirmed_net < threshold:
                logging.critical(
                    "Ignoring unconfirmed catastrophe: stream net %.2f >= %.2f, broker net %.2f.",
                    live_net,
                    threshold,
                    confirmed_net,
                )
                state_manager.update_many({
                    "feed_status": "UNCONFIRMED_CATASTROPHE:REST_DISAGREE",
                    "last_live_prices": confirmed_prices,
                    "live_net_premium": round(confirmed_net, 4),
                    "current_profit_pct": round((entry_net - confirmed_net) / entry_net, 6) if entry_net > 0 else 0.0,
                })
                return False, {}
            current_prices = confirmed_prices
            live_net = confirmed_net
            state_manager.update_many({
                "last_live_prices": current_prices,
                "live_net_premium": round(live_net, 4),
                "current_profit_pct": round((entry_net - live_net) / entry_net, 6) if entry_net > 0 else 0.0,
            })

        logging.critical(
            "CATASTROPHE KILL: live net %.2f >= %.2f.",
            live_net,
            threshold,
        )
        return "CATASTROPHE_KILL", current_prices

    now = datetime.datetime.now()
    if now.hour > 15 or (now.hour == 15 and now.minute >= 25):
        return _eod_signal(state, current_prices)

    use_point_drift = drift_points_threshold > 0
    drift_triggered = (
        drift_points >= float(drift_points_threshold)
        if use_point_drift
        else drift_ratio >= drift_threshold
    )
    if drift_triggered:
        if config.EMERGENCY_EXIT_CONFIRMATION_ENABLED:
            confirmed_spot = _fresh_spot_snapshot(index_symbol)
            if not confirmed_spot:
                state_manager.update_state("feed_status", "UNCONFIRMED_ATM_DRIFT:NO_REST_SPOT")
                raise ValueError("ATM drift could not be confirmed with fresh broker spot")
            confirmed_ratio = _atm_drift_ratio(confirmed_spot, strikes)
            confirmed_points = _atm_drift_points(confirmed_spot, strikes)
            confirmed_triggered = (
                confirmed_points >= float(drift_points_threshold)
                if use_point_drift
                else confirmed_ratio >= drift_threshold
            )
            if not confirmed_triggered:
                logging.critical(
                    "Ignoring unconfirmed ATM drift: stream %.2fx / %.2f pts, broker %.2fx / %.2f pts.",
                    drift_ratio,
                    drift_points,
                    confirmed_ratio,
                    confirmed_points,
                )
                state_manager.update_many({
                    "feed_status": "UNCONFIRMED_ATM_DRIFT:REST_DISAGREE",
                    "atm_drift_ratio": round(confirmed_ratio, 3),
                    "atm_drift_points": round(confirmed_points, 2),
                    "last_spot": round(confirmed_spot, 2),
                })
                return False, {}
            state_manager.update_many({
                "atm_drift_ratio": round(confirmed_ratio, 3),
                "atm_drift_points": round(confirmed_points, 2),
                "last_spot": round(confirmed_spot, 2),
            })
            drift_ratio = confirmed_ratio
            drift_points = confirmed_points

        if use_point_drift:
            logging.critical("ATM DRIFT EJECTOR: %.2f pts >= %.2f pts.", drift_points, float(drift_points_threshold))
        else:
            logging.critical("ATM DRIFT EJECTOR: %.2fx >= %.2fx.", drift_ratio, drift_threshold)
        return "ATM_DRIFT", current_prices

    if config.SNIPER_TARGETS_ENABLED and current_profit_pct >= target_pct / 100.0:
        logging.critical("SNIPER TARGET HIT at %.2f%%. Exiting.", current_profit_pct * 100)
        return "SNIPER_TARGET", current_prices

    return False, {}
