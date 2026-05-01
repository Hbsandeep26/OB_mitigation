import datetime
import logging
import os
import time

import config
import state_manager
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


def get_vix_session_profile(live_vix):
    profile = {
        "name": "SNIPER_SHIELD",
        "wing_delta": config.SNIPER_WING_DELTA,
        "target_pct": config.SNIPER_TARGET_PCT,
    }
    logging.info(
        "Sniper & Shield profile selected: VIX=%.2f, wing delta=%s, target=%s%%",
        live_vix,
        profile["wing_delta"],
        profile["target_pct"],
    )
    return profile


def _find_wing_by_delta(option_chain_data, atm_strike, atm_premium, side, target_delta):
    target_delta_decimal = target_delta / 100.0

    best_delta = None
    farthest_usable = None

    for strike_data in option_chain_data:
        strike = strike_data.get("strike_price", 0)

        if side == "CE":
            if strike <= atm_strike:
                continue
            option_info = strike_data.get("call_options", {})
        else:
            if strike >= atm_strike:
                continue
            option_info = strike_data.get("put_options", {})

        if not option_info:
            continue

        ltp = option_info.get("market_data", {}).get("ltp", 0)
        if ltp <= 0:
            continue

        instrument_key = option_info.get("instrument_key", "")
        option_delta = option_info.get("greeks", {}).get("delta")

        if option_delta not in (None, 0):
            delta_distance = abs(abs(option_delta) - target_delta_decimal)
            if best_delta is None or delta_distance < best_delta[0]:
                best_delta = (delta_distance, instrument_key, ltp, strike)
        else:
            strike_distance = abs(float(strike) - float(atm_strike))
            candidate = (strike_distance, -float(ltp), instrument_key, ltp, strike)
            if farthest_usable is None or candidate > farthest_usable:
                farthest_usable = candidate

    selected = best_delta
    if not selected:
        if not farthest_usable:
            return None, 0, 0
        _, _, best_key, best_ltp, best_strike = farthest_usable
        logging.info("Wing %s: Strike %s, LTP %.2f (via farthest usable fallback)", side, best_strike, best_ltp)
        return best_key, best_ltp, best_strike

    _, best_key, best_ltp, best_strike = selected
    logging.info("Wing %s: Strike %s, LTP %.2f (via Greeks delta, target delta=%s)", side, best_strike, best_ltp, target_delta)
    return best_key, best_ltp, best_strike


def calculate_iron_butterfly_legs(index_symbol, spot_price, option_chain_data, wing_delta=None):
    wing_delta = config.SNIPER_WING_DELTA if wing_delta is None else wing_delta
    logging.info("Calculating Iron Butterfly strikes & prices (Wing delta=%s)...", wing_delta)
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

    buy_ce_key, buy_ce_ltp, buy_ce_strike = _find_wing_by_delta(option_chain_data, atm_strike, atm_ce_ltp, "CE", wing_delta)
    buy_pe_key, buy_pe_ltp, buy_pe_strike = _find_wing_by_delta(option_chain_data, atm_strike, atm_pe_ltp, "PE", wing_delta)

    if not buy_ce_key or not buy_pe_key:
        logging.warning("Delta-based wing selection failed. Falling back to premium percentage method.")
        target_ce_buy = atm_ce_ltp * config.WING_PERCENT
        target_pe_buy = atm_pe_ltp * config.WING_PERCENT
        best_ce_diff, best_pe_diff = float("inf"), float("inf")
        buy_ce_key, buy_pe_key = "", ""
        buy_ce_ltp, buy_pe_ltp = 0, 0
        buy_ce_strike, buy_pe_strike = 0, 0

        for strike_data in option_chain_data:
            strike = strike_data.get("strike_price")
            call_info = strike_data.get("call_options", {})
            put_info = strike_data.get("put_options", {})

            if strike > atm_strike and call_info:
                ce_ltp = call_info.get("market_data", {}).get("ltp", 0)
                diff = abs(ce_ltp - target_ce_buy)
                if ce_ltp > 0 and diff < best_ce_diff:
                    best_ce_diff = diff
                    buy_ce_key = call_info.get("instrument_key")
                    buy_ce_ltp = ce_ltp
                    buy_ce_strike = strike

            if strike < atm_strike and put_info:
                pe_ltp = put_info.get("market_data", {}).get("ltp", 0)
                diff = abs(pe_ltp - target_pe_buy)
                if pe_ltp > 0 and diff < best_pe_diff:
                    best_pe_diff = diff
                    buy_pe_key = put_info.get("instrument_key")
                    buy_pe_ltp = pe_ltp
                    buy_pe_strike = strike

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


def _atm_drift_ratio(live_spot, strikes):
    atm_strike = strikes.get("sell_ce", 0.0)
    if live_spot <= 0 or atm_strike <= 0:
        return 0.0

    ce_wing_width = abs(strikes.get("buy_ce", atm_strike) - atm_strike)
    pe_wing_width = abs(atm_strike - strikes.get("buy_pe", atm_strike))
    avg_wing_width = (ce_wing_width + pe_wing_width) / 2.0
    if avg_wing_width <= 0:
        return 0.0
    return abs(live_spot - atm_strike) / avg_wing_width


def risk_management_evaluator(live_data, legs):
    state = state_manager.load_state()
    if not state or "entry_prices" not in state:
        return False, {}

    entries = state["entry_prices"]
    current_prices = {
        "sell_ce": entries["sell_ce"],
        "sell_pe": entries["sell_pe"],
        "buy_ce": entries["buy_ce"],
        "buy_pe": entries["buy_pe"]
    }

    manual_exit_file = os.path.join(BASE_DIR, "manual_exit_flag.txt")
    if os.path.exists(manual_exit_file):
        with open(manual_exit_file, "r") as f:
            if f.read().strip() == "TRUE":
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

    live_sell_ce = _fresh_ltp(live_data, legs["sell_ce"], "sell_ce")
    live_sell_pe = _fresh_ltp(live_data, legs["sell_pe"], "sell_pe")
    live_buy_ce = _fresh_ltp(live_data, legs["buy_ce"], "buy_ce")
    live_buy_pe = _fresh_ltp(live_data, legs["buy_pe"], "buy_pe")
    current_prices = {"sell_ce": live_sell_ce, "sell_pe": live_sell_pe, "buy_ce": live_buy_ce, "buy_pe": live_buy_pe}
    state_manager.update_state("feed_status", "LIVE")

    index_symbol = state.get("index_symbol", "NIFTY")
    spot_key = "NSE_INDEX|Nifty 50" if index_symbol == "NIFTY" else "BSE_INDEX|SENSEX"
    live_spot = live_data.get(spot_key, {}).get("ltp", 0.0)
    strikes = state.get("strikes", {})
    entry_net = float(state.get("entry_net_premium") or _entry_net(entries))
    live_net = _live_net(current_prices)
    current_profit_pct = (entry_net - live_net) / entry_net if entry_net > 0 else 0.0
    drift_ratio = _atm_drift_ratio(live_spot, strikes)
    sniper_state = state.get("sniper_state", "INITIAL")

    state_manager.update_many({
        "sniper_state": sniper_state,
        "entry_net_premium": round(entry_net, 4),
        "sniper_target_pct": config.SNIPER_TARGET_PCT,
        "level_up_target_pct": config.SNIPER_LEVEL_UP_TARGET_PCT,
        "level_up_floor_pct": config.SNIPER_LEVEL_UP_FLOOR_PCT,
        "atm_drift_ratio": round(drift_ratio, 3),
        "live_net_premium": round(live_net, 4),
        "current_profit_pct": round(current_profit_pct, 6),
        "catastrophe_threshold": round(entry_net * config.SNIPER_CATASTROPHE_MULTIPLIER, 4),
        "last_spot": round(live_spot, 2) if live_spot else state.get("last_spot", 0),
    })

    if entry_net > 0 and live_net >= entry_net * config.SNIPER_CATASTROPHE_MULTIPLIER:
        logging.critical(
            "CATASTROPHE KILL: live net %.2f >= %.2f.",
            live_net,
            entry_net * config.SNIPER_CATASTROPHE_MULTIPLIER,
        )
        return "CATASTROPHE_KILL", current_prices

    now = datetime.datetime.now()
    if now.hour > 15 or (now.hour == 15 and now.minute >= 15):
        btst_file = os.path.join(BASE_DIR, "btst_flag.txt")
        btst_enabled = False
        if os.path.exists(btst_file):
            with open(btst_file, "r") as f:
                btst_enabled = f.read().strip() == "TRUE"
        if btst_enabled:
            is_healthy, diagnosis = evaluate_btst_health(live_data, legs, entries)
            if is_healthy:
                return False, {}
            else:
                logging.critical("BTST position unhealthy: %s. Forcing BTST recenter.", diagnosis)
                return "BTST_RECENTER", current_prices
        logging.critical("END OF DAY (15:15) REACHED. Forcing Square Off.")
        return "TIME_EXIT", current_prices

    if drift_ratio >= config.SNIPER_DRIFT_EJECT_RATIO:
        logging.critical("ATM DRIFT EJECTOR: %.2fx >= %.2fx.", drift_ratio, config.SNIPER_DRIFT_EJECT_RATIO)
        return "ATM_DRIFT", current_prices

    if sniper_state == "LEVEL_UP":
        if current_profit_pct >= config.SNIPER_LEVEL_UP_TARGET_PCT / 100.0:
            logging.critical("LEVEL UP TARGET HIT at %.2f%%.", current_profit_pct * 100)
            return "LEVEL_UP_TARGET", current_prices
        if current_profit_pct <= config.SNIPER_LEVEL_UP_FLOOR_PCT / 100.0:
            logging.critical("LEVEL UP FLOOR HIT at %.2f%%.", current_profit_pct * 100)
            return "LEVEL_UP_FLOOR", current_prices
        return False, {}

    if current_profit_pct >= config.SNIPER_TARGET_PCT / 100.0:
        if drift_ratio > config.SNIPER_PINNED_DRIFT_RATIO:
            logging.critical("SNIPER TARGET HIT with drift %.2fx. Exiting.", drift_ratio)
            return "SNIPER_TARGET", current_prices

        state_manager.update_state("sniper_state", "LEVEL_UP")
        logging.critical("SNIPER TARGET HIT while pinned. Level Up state activated.")
        return False, {}

    return False, {}
