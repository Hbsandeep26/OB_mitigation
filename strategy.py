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
    if live_vix < config.VIX_LOW_THRESHOLD:
        profile = {"name": "LOW_VIX", "wing_delta": 8, "target_pct": 12}
    elif live_vix > config.VIX_HIGH_THRESHOLD:
        profile = {"name": "HIGH_VIX", "wing_delta": 12, "target_pct": 25}
    else:
        profile = {"name": "MID_VIX", "wing_delta": 10, "target_pct": 20}

    logging.info(
        "VIX Profile Selected: %s (VIX=%.2f) -> Wing delta=%s, Target=%s%%",
        profile["name"],
        live_vix,
        profile["wing_delta"],
        profile["target_pct"],
    )
    return profile


def _find_wing_by_delta(option_chain_data, atm_strike, atm_premium, side, target_delta):
    target_delta_decimal = target_delta / 100.0
    target_premium = atm_premium * (target_delta / 50.0)

    best_delta = None
    best_premium = None

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
            premium_distance = abs(ltp - target_premium)
            if best_premium is None or premium_distance < best_premium[0]:
                best_premium = (premium_distance, instrument_key, ltp, strike)

    selected = best_delta or best_premium
    if not selected:
        return None, 0, 0

    method = "Greeks delta" if best_delta else "Premium Proxy"
    _, best_key, best_ltp, best_strike = selected
    logging.info("Wing %s: Strike %s, LTP %.2f (via %s, target delta=%s)", side, best_strike, best_ltp, method, target_delta)
    return best_key, best_ltp, best_strike


def calculate_iron_butterfly_legs(index_symbol, spot_price, option_chain_data, wing_delta=10):
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
                logging.critical("BTST position unhealthy: %s. Forcing TIME_EXIT.", diagnosis)
                return "BTST_RECENTER", current_prices
        logging.critical("END OF DAY (15:15) REACHED. Forcing Square Off.")
        return "TIME_EXIT", current_prices

    entry_net = (entries["sell_ce"] + entries["sell_pe"]) - (entries["buy_ce"] + entries["buy_pe"])
    live_net = (live_sell_ce + live_sell_pe) - (live_buy_ce + live_buy_pe)
    current_profit_pct = (entry_net - live_net) / entry_net if entry_net > 0 else 0.0

    net_sl_breach_count = state.get("net_sl_breach_count", 0)
    net_stop = entry_net * config.NET_STOP_LOSS_MULTIPLIER
    if live_net >= net_stop:
        net_sl_breach_count += 1
        state_manager.update_state("net_sl_breach_count", net_sl_breach_count)
        logging.warning("Net premium SL warning: %.2f >= %.2f (%s/3)", live_net, net_stop, net_sl_breach_count)
        if net_sl_breach_count >= 3:
            logging.critical("CONFIRMED NET STOP LOSS. Exiting.")
            return "STOP_LOSS", current_prices
        return False, {}
    elif net_sl_breach_count > 0:
        state_manager.update_state("net_sl_breach_count", 0)

    applied_target_pct = state.get("applied_target_pct", config.get_target_profit_pct())
    base_target_pct = applied_target_pct / 100.0

    index_symbol = state.get("index_symbol", "NIFTY")
    spot_key = "NSE_INDEX|Nifty 50" if index_symbol == "NIFTY" else "BSE_INDEX|SENSEX"
    vix_key = "NSE_INDEX|India VIX"
    live_spot = live_data.get(spot_key, {}).get("ltp", 0.0)
    live_vix = live_data.get(vix_key, {}).get("ltp", 15.0)
    strikes = state.get("strikes", {})
    atm_strike = strikes.get("sell_ce", 0.0)
    if live_spot > 0:
        state_manager.update_state("last_spot", round(live_spot, 2))

    atm_grace_allowed = False
    if live_spot > 0 and atm_strike > 0 and strikes:
        ce_wing_width = abs(strikes.get("buy_ce", atm_strike) - atm_strike)
        pe_wing_width = abs(atm_strike - strikes.get("buy_pe", atm_strike))
        avg_wing_width = (ce_wing_width + pe_wing_width) / 2.0
        if avg_wing_width > 0:
            drift_distance = abs(live_spot - atm_strike)
            drift_ratio = drift_distance / avg_wing_width
            state_manager.update_state("atm_drift_ratio", round(drift_ratio, 3))
            atm_grace_allowed = drift_ratio <= config.PROFIT_LOCK_ATM_GRACE_RATIO
            if drift_ratio > config.ATM_DRIFT_MULTIPLIER:
                logging.critical("ATM DRIFT DETECTED: %.2fx wing width. Forcing exit.", drift_ratio)
                return "ATM_DRIFT", current_prices

    profit_hwm = state.get("profit_high_water_mark", 0.0)
    trail_active = state.get("trail_active", False)
    trail_floor = state.get("trail_floor", 0.0)

    if current_profit_pct > profit_hwm:
        profit_hwm = current_profit_pct
        state_manager.update_state("profit_high_water_mark", round(profit_hwm, 6))

    profit_lock_tier = state.get("profit_lock_tier", 0)
    profit_lock_floor = state.get("profit_lock_floor", 0.0)
    
    if current_profit_pct >= base_target_pct and profit_lock_tier < 4:
        spot_distance = abs(live_spot - atm_strike) / atm_strike if live_spot > 0 and atm_strike > 0 else float('inf')
        daily_expected_move = live_vix / 19.1
        dynamic_zone_tolerance = (daily_expected_move / 100.0) * 0.60
        
        if spot_distance <= dynamic_zone_tolerance:
            new_floor = base_target_pct * config.TRAIL_LOCK_FLOOR_PCT
            state_manager.update_many({
                "profit_lock_tier": 4,
                "profit_lock_floor": round(new_floor, 6),
                "trail_active": True,
                "trail_floor": round(new_floor, 6),
            })
            profit_lock_floor = new_floor
            trail_active = True
            trail_floor = new_floor
            logging.critical("Base target hit inside safe zone. Ratchet trail active at Tier 4.")
        else:
            logging.critical("Target reached outside safe zone. Taking profit.")
            return "TAKE_PROFIT", current_prices
            
    elif current_profit_pct >= base_target_pct * config.PROFIT_LOCK_TIER3_TRIGGER and profit_lock_tier < 3:
        new_floor = base_target_pct * config.PROFIT_LOCK_TIER3_FLOOR
        state_manager.update_many({
            "profit_lock_tier": 3,
            "profit_lock_floor": round(new_floor, 6),
            "trail_active": True,
            "trail_floor": round(new_floor, 6),
        })
        profit_lock_floor = new_floor
        trail_active = True
        trail_floor = new_floor
    elif current_profit_pct >= base_target_pct * config.PROFIT_LOCK_TIER2_TRIGGER and profit_lock_tier < 2:
        new_floor = base_target_pct * config.PROFIT_LOCK_TIER2_FLOOR
        state_manager.update_many({
            "profit_lock_tier": 2,
            "profit_lock_floor": round(new_floor, 6),
        })
        profit_lock_floor = new_floor
    elif current_profit_pct >= base_target_pct * config.PROFIT_LOCK_TIER1_TRIGGER and profit_lock_tier < 1:
        new_floor = base_target_pct * config.PROFIT_LOCK_TIER1_FLOOR
        state_manager.update_many({
            "profit_lock_tier": 1,
            "profit_lock_floor": round(new_floor, 6),
        })
        profit_lock_floor = new_floor

    if profit_lock_floor > 0 and current_profit_pct <= profit_lock_floor:
        grace_started = state.get("profit_lock_grace_started_ts")
        now_ts = time.time()
        if atm_grace_allowed:
            if not grace_started:
                state_manager.update_state("profit_lock_grace_started_ts", now_ts)
                logging.warning(
                    "Profit lock floor touched near ATM. Holding for %.0fs grace before exit.",
                    config.PROFIT_LOCK_ATM_GRACE_SECONDS,
                )
                return False, {}
            if now_ts - float(grace_started) < config.PROFIT_LOCK_ATM_GRACE_SECONDS:
                logging.info("Profit lock ATM grace active for %.1fs.", now_ts - float(grace_started))
                return False, {}

        logging.critical("PROFIT LOCK FLOOR BREACHED (Tier %d). Exiting at %.2f%%",
                         profit_lock_tier, current_profit_pct * 100)
        state_manager.update_state("profit_lock_grace_started_ts", None)
        return "TAKE_PROFIT", current_prices
    elif state.get("profit_lock_grace_started_ts"):
        state_manager.update_state("profit_lock_grace_started_ts", None)

    if trail_active:
        new_trail_floor = max(trail_floor, profit_hwm * config.TRAIL_RATCHET_FACTOR)
        if new_trail_floor > trail_floor:
            trail_floor = new_trail_floor
            state_manager.update_state("trail_floor", round(trail_floor, 6))
            logging.info("Ratchet Trail: HWM=%.2f%%, Floor=%.2f%%", profit_hwm * 100, trail_floor * 100)

        if current_profit_pct <= trail_floor:
            logging.critical("RATCHET TRAIL STOP HIT. Locking %.2f%% profit.", trail_floor * 100)
            return "TAKE_PROFIT", current_prices

        greedy_target = base_target_pct * 1.5
        if current_profit_pct >= greedy_target:
            logging.critical("MAX RATCHET REACHED. Taking profit at %.2f%%.", current_profit_pct * 100)
            return "TAKE_PROFIT", current_prices

    entry_sell_ce = float(entries["sell_ce"])
    entry_sell_pe = float(entries["sell_pe"])
    limit_ce = entry_sell_ce * 2.0
    limit_pe = entry_sell_pe * 2.0
    sl_breach_count = state.get("sl_breach_count", 0)

    if float(live_sell_ce) >= limit_ce or float(live_sell_pe) >= limit_pe:
        sl_breach_count += 1
        state_manager.update_state("sl_breach_count", sl_breach_count)
        logging.warning("Leg SL warning. Confirmation count: %s/3", sl_breach_count)
        if sl_breach_count >= 3:
            logging.critical("CONFIRMED LEG STOP LOSS. Exiting.")
            return "STOP_LOSS", current_prices
        return False, {}

    if sl_breach_count > 0:
        state_manager.update_state("sl_breach_count", 0)

    return False, {}
