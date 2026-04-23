# strategy.py
import config
import logging
import state_manager
import time
import os
import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ============================================================================
# VIX ADAPTIVE SESSION PROFILES
# ============================================================================

def get_vix_session_profile(live_vix):
    """
    Determines the session's trading profile based on the current India VIX.
    
    Low VIX  (<13): Premium is cheap → tighten wings (8δ), take quick 12% decay
    Mid VIX (13-18): Normal conditions → standard wings (10δ), target 20%
    High VIX (>18): Premium is fat but dangerous → wide wings (12δ), target 25%
    
    Returns a dict with 'name', 'wing_delta', and 'target_pct'.
    """
    if live_vix < config.VIX_LOW_THRESHOLD:
        profile = {
            "name": "LOW_VIX",
            "wing_delta": 8,
            "target_pct": 12
        }
    elif live_vix > config.VIX_HIGH_THRESHOLD:
        profile = {
            "name": "HIGH_VIX",
            "wing_delta": 12,
            "target_pct": 25
        }
    else:
        profile = {
            "name": "MID_VIX",
            "wing_delta": 10,
            "target_pct": 20
        }
    
    logging.info(f"📊 VIX Profile Selected: {profile['name']} (VIX={live_vix:.2f}) → Wing δ={profile['wing_delta']}, Target={profile['target_pct']}%")
    return profile


# ============================================================================
# DELTA-BASED WING SELECTION
# ============================================================================

def _find_wing_by_delta(option_chain_data, atm_strike, atm_premium, side, target_delta):
    """
    Finds the optimal protective wing strike using delta-based selection.
    
    Primary: Uses greeks.delta from the chain if available.
    Fallback: Premium-percentage approximation when greeks are missing.
        - Maps delta to premium ratio: wing_premium ≈ (target_delta / 50) * atm_premium
        - 8δ ≈ 16% of ATM premium, 10δ ≈ 20%, 12δ ≈ 24%
    
    Args:
        option_chain_data: Full option chain from Upstox
        atm_strike: The ATM strike price
        atm_premium: The ATM option LTP (CE or PE side)
        side: "CE" or "PE"
        target_delta: Target delta value (8, 10, or 12)
    
    Returns:
        (instrument_key, ltp, strike_price) or (None, 0, 0)
    """
    target_delta_decimal = target_delta / 100.0
    # Premium fallback: approximate premium for target delta
    target_premium_pct = target_delta / 50.0  # 8→0.16, 10→0.20, 12→0.24
    target_premium = atm_premium * target_premium_pct
    
    best_key = None
    best_ltp = 0
    best_strike = 0
    best_score = float('inf')
    
    greeks_available = False
    
    for strike_data in option_chain_data:
        strike = strike_data.get('strike_price', 0)
        
        if side == "CE":
            if strike <= atm_strike:
                continue  # Only OTM calls (above ATM)
            option_info = strike_data.get('call_options', {})
        else:  # PE
            if strike >= atm_strike:
                continue  # Only OTM puts (below ATM)
            option_info = strike_data.get('put_options', {})
        
        if not option_info:
            continue
            
        ltp = option_info.get('market_data', {}).get('ltp', 0)
        if ltp <= 0:
            continue
        
        instrument_key = option_info.get('instrument_key', '')
        
        # --- PRIMARY: Try greeks-based delta matching ---
        greeks = option_info.get('greeks', {})
        option_delta = greeks.get('delta', None)
        
        if option_delta is not None and option_delta != 0:
            greeks_available = True
            delta_distance = abs(abs(option_delta) - target_delta_decimal)
            
            if delta_distance < best_score:
                best_score = delta_distance
                best_key = instrument_key
                best_ltp = ltp
                best_strike = strike
        else:
            # --- FALLBACK: Premium-percentage approximation ---
            if not greeks_available:
                premium_distance = abs(ltp - target_premium)
                if premium_distance < best_score:
                    best_score = premium_distance
                    best_key = instrument_key
                    best_ltp = ltp
                    best_strike = strike
    
    method = "Greeks δ" if greeks_available else "Premium Proxy"
    if best_key:
        logging.info(f"  Wing {side}: Strike {best_strike}, LTP ₹{best_ltp:.2f} (via {method}, target δ={target_delta})")
    
    return best_key, best_ltp, best_strike


# ============================================================================
# IRON BUTTERFLY LEG CALCULATOR
# ============================================================================

def calculate_iron_butterfly_legs(index_symbol, spot_price, option_chain_data, wing_delta=10):
    """
    Calculates the full Iron Butterfly structure with delta-based wing selection.
    
    Args:
        index_symbol: "NIFTY" or "SENSEX"
        spot_price: Current spot price
        option_chain_data: Upstox option chain
        wing_delta: Target delta for wings from VIX profile (8, 10, or 12)
    """
    logging.info(f"Calculating Iron Butterfly strikes & prices (Wing δ={wing_delta})...")
    interval = 50 if index_symbol == "NIFTY" else 100
    atm_strike = round(spot_price / interval) * interval

    atm_ce_ltp, atm_pe_ltp = 0, 0
    sell_ce_key, sell_pe_key = "", ""

    for strike_data in option_chain_data:
        if strike_data.get('strike_price') == atm_strike:
            call_info, put_info = strike_data.get('call_options', {}), strike_data.get('put_options', {})
            if call_info and put_info:
                sell_ce_key, atm_ce_ltp = call_info.get('instrument_key'), call_info.get('market_data', {}).get('ltp', 0)
                sell_pe_key, atm_pe_ltp = put_info.get('instrument_key'), put_info.get('market_data', {}).get('ltp', 0)
            break

    if not sell_ce_key or not sell_pe_key: 
        return None, None, None

    # --- DELTA-BASED WING SELECTION (replaces fixed WING_PERCENT) ---
    logging.info(f"Selecting protective wings via delta-based method (target δ={wing_delta})...")
    
    buy_ce_key, buy_ce_ltp, buy_ce_strike = _find_wing_by_delta(
        option_chain_data, atm_strike, atm_ce_ltp, "CE", wing_delta
    )
    buy_pe_key, buy_pe_ltp, buy_pe_strike = _find_wing_by_delta(
        option_chain_data, atm_strike, atm_pe_ltp, "PE", wing_delta
    )

    if not buy_ce_key or not buy_pe_key:
        logging.warning("⚠️ Delta-based wing selection failed. Falling back to legacy WING_PERCENT method...")
        # --- LEGACY FALLBACK: Original 5% wing selection ---
        target_ce_buy = atm_ce_ltp * config.WING_PERCENT
        target_pe_buy = atm_pe_ltp * config.WING_PERCENT
        
        best_ce_diff, best_pe_diff = float('inf'), float('inf')
        buy_ce_key, buy_pe_key = "", ""
        buy_ce_ltp, buy_pe_ltp = 0, 0
        buy_ce_strike, buy_pe_strike = 0, 0

        for strike_data in option_chain_data:
            strike = strike_data.get('strike_price')
            call_info, put_info = strike_data.get('call_options', {}), strike_data.get('put_options', {})

            if strike > atm_strike and call_info:
                ce_ltp = call_info.get('market_data', {}).get('ltp', 0)
                if ce_ltp > 0 and abs(ce_ltp - target_ce_buy) < best_ce_diff:
                    best_ce_diff = abs(ce_ltp - target_ce_buy)
                    buy_ce_key = call_info.get('instrument_key')
                    buy_ce_ltp = ce_ltp
                    buy_ce_strike = strike

            if strike < atm_strike and put_info:
                pe_ltp = put_info.get('market_data', {}).get('ltp', 0)
                if pe_ltp > 0 and abs(pe_ltp - target_pe_buy) < best_pe_diff:
                    best_pe_diff = abs(pe_ltp - target_pe_buy)
                    buy_pe_key = put_info.get('instrument_key')
                    buy_pe_ltp = pe_ltp
                    buy_pe_strike = strike

    if not buy_ce_key or not buy_pe_key:
        logging.warning("⚠️ Upstox Option Chain is missing deep OTM strikes. Cannot build complete Iron Butterfly. Aborting calculation.")
        return None, None, None

    legs = {"sell_ce": sell_ce_key, "sell_pe": sell_pe_key, "buy_ce": buy_ce_key, "buy_pe": buy_pe_key}
    prices = {"sell_ce": atm_ce_ltp, "sell_pe": atm_pe_ltp, "buy_ce": buy_ce_ltp, "buy_pe": buy_pe_ltp}
    
    strikes = {
        "sell_ce": atm_strike,         
        "sell_pe": atm_strike,         
        "buy_ce": buy_ce_strike,       
        "buy_pe": buy_pe_strike        
    }

    logging.info(f"Selected Execution Legs: {legs}")
    logging.info(f"Target Entry Prices: {prices}")
    logging.info(f"Selected Strikes: {strikes}")

    return legs, prices, strikes


# ============================================================================
# RISK MANAGEMENT EVALUATOR (THE BRAIN)
# ============================================================================

def risk_management_evaluator(live_data, legs):
    state = state_manager.load_state()
    if not state or 'entry_prices' not in state:
        return False, {}

    entries = state['entry_prices']

    live_sell_ce = live_data.get(legs['sell_ce'], {}).get('ltp', entries['sell_ce'])
    live_sell_pe = live_data.get(legs['sell_pe'], {}).get('ltp', entries['sell_pe'])
    live_buy_ce = live_data.get(legs['buy_ce'], {}).get('ltp', entries['buy_ce'])
    live_buy_pe = live_data.get(legs['buy_pe'], {}).get('ltp', entries['buy_pe'])

    current_prices = {
        'sell_ce': live_sell_ce,
        'sell_pe': live_sell_pe,
        'buy_ce': live_buy_ce,
        'buy_pe': live_buy_pe
    }

    # --- MANUAL EXIT CHECK ---
    manual_exit_file = os.path.join(BASE_DIR, "manual_exit_flag.txt")
    if os.path.exists(manual_exit_file):
        with open(manual_exit_file, "r") as f:
            if f.read().strip() == "TRUE":
                logging.critical("🛑 MANUAL EXIT TRIGGERED FROM UI! Forcing Square Off.")
                os.remove(manual_exit_file)
                return "MANUAL_EXIT", current_prices 

    # --- END OF DAY / BTST CHECK ---
    now = datetime.datetime.now()
    if now.hour > 15 or (now.hour == 15 and now.minute >= 15):
        btst_file = os.path.join(BASE_DIR, "btst_flag.txt")
        btst_enabled = False
        if os.path.exists(btst_file):
            with open(btst_file, "r") as f:
                btst_enabled = (f.read().strip() == "TRUE")
                
        if btst_enabled:
            return False, {} 
            
        logging.critical("⏰ END OF DAY (15:15) REACHED! Forcing Square Off.")
        return "TIME_EXIT", current_prices

    # --- PREMIUM CALCULATIONS ---
    entry_net = (entries['sell_ce'] + entries['sell_pe']) - (entries['buy_ce'] + entries['buy_pe'])
    live_net = (live_sell_ce + live_sell_pe) - (live_buy_ce + live_buy_pe)
    
    # Current profit as a fraction of entry premium
    if entry_net > 0:
        current_profit_pct = (entry_net - live_net) / entry_net
    else:
        current_profit_pct = 0.0

    # --- VIX-ADAPTIVE TARGET: Read from trade_state (set at entry by main.py) ---
    # If user manually hot-swapped via dashboard, that takes priority
    applied_target_pct = state.get("applied_target_pct", config.get_target_profit_pct())
    base_target_pct = applied_target_pct / 100.0

    # --- SPOT & STRIKE DATA FOR DRIFT CHECK ---
    index_symbol = state.get('index_symbol', 'NIFTY')
    spot_key = "NSE_INDEX|Nifty 50" if index_symbol == "NIFTY" else "BSE_INDEX|SENSEX"
    vix_key = "NSE_INDEX|India VIX"
    
    live_spot = live_data.get(spot_key, {}).get('ltp', 0.0)
    live_vix = live_data.get(vix_key, {}).get('ltp', 15.0) 
    strikes = state.get('strikes', {})
    atm_strike = strikes.get('sell_ce', 0.0)

    # ================================================================
    # CHECK 1: ATM DRIFT GUARD (Stale Strike Detection)
    # ================================================================
    if live_spot > 0 and atm_strike > 0 and strikes:
        ce_wing_width = abs(strikes.get('buy_ce', atm_strike) - atm_strike)
        pe_wing_width = abs(atm_strike - strikes.get('buy_pe', atm_strike))
        avg_wing_width = (ce_wing_width + pe_wing_width) / 2.0
        
        if avg_wing_width > 0:
            drift_distance = abs(live_spot - atm_strike)
            drift_ratio = drift_distance / avg_wing_width
            
            # Persist drift for dashboard display
            state_manager.update_state("atm_drift_ratio", round(drift_ratio, 3))
            
            if drift_ratio > config.ATM_DRIFT_MULTIPLIER:
                logging.critical(
                    f"🌊 ATM DRIFT DETECTED! Spot has drifted {drift_ratio:.2f}x wing width from ATM. "
                    f"(Spot={live_spot:.2f}, ATM={atm_strike}, Drift={drift_distance:.0f} pts, "
                    f"Wing Width={avg_wing_width:.0f}, Limit={config.ATM_DRIFT_MULTIPLIER}x). "
                    f"Structure is broken — forcing exit."
                )
                return "ATM_DRIFT", current_prices

    # ================================================================
    # CHECK 2: RATCHET TRAILING STOP SYSTEM
    # ================================================================
    profit_hwm = state.get("profit_high_water_mark", 0.0)
    trail_active = state.get("trail_active", False)
    trail_floor = state.get("trail_floor", 0.0)
    
    # Update high-water mark if we have a new peak
    if current_profit_pct > profit_hwm:
        profit_hwm = current_profit_pct
        state_manager.update_state("profit_high_water_mark", round(profit_hwm, 6))
    
    if trail_active:
        # --- RATCHET: Trail floor climbs with the high-water mark ---
        new_trail_floor = max(trail_floor, profit_hwm * config.TRAIL_RATCHET_FACTOR)
        
        if new_trail_floor > trail_floor:
            trail_floor = new_trail_floor
            state_manager.update_state("trail_floor", round(trail_floor, 6))
            logging.info(f"📈 Ratchet Trail: HWM={profit_hwm*100:.2f}%, Floor raised to {trail_floor*100:.2f}%")
        
        # Check if profit has fallen back to the trail floor
        if current_profit_pct <= trail_floor:
            locked_pct = trail_floor * 100
            logging.critical(
                f"🛡️ RATCHET TRAIL STOP HIT! Profit retreated to floor. "
                f"Locking in {locked_pct:.2f}% profit (HWM was {profit_hwm*100:.2f}%)."
            )
            return "TAKE_PROFIT", current_prices
        
        # Check for greedy maximum (profit hit 2x base target — take it!)
        greedy_target = base_target_pct * 2.0
        if current_profit_pct >= greedy_target:
            logging.critical(
                f"🌟 MAX RATCHET REACHED! Profit at {current_profit_pct*100:.2f}% "
                f"(2x base target of {base_target_pct*100:.2f}%). Taking the windfall!"
            )
            return "TAKE_PROFIT", current_prices
    
    else:
        # --- Trail not yet active: check if we've hit the base target ---
        target_exit_premium = entry_net * (1.0 - base_target_pct)
        
        if live_net <= target_exit_premium:
            # Target reached — should we activate the trail or exit immediately?
            if live_spot > 0 and atm_strike > 0:
                spot_distance = abs(live_spot - atm_strike) / atm_strike
                daily_expected_move = live_vix / 19.1
                dynamic_zone_tolerance = (daily_expected_move / 100.0) * 0.20 
                
                if spot_distance <= dynamic_zone_tolerance:
                    # Spot is safe near ATM → activate ratchet trail for more profit
                    initial_floor = base_target_pct * config.TRAIL_LOCK_FLOOR_PCT
                    
                    state_manager.update_state("trail_active", True)
                    state_manager.update_state("trail_floor", round(initial_floor, 6))
                    state_manager.update_state("profit_high_water_mark", round(current_profit_pct, 6))
                    
                    logging.critical(
                        f"🚨 ZONE DEFENSE (VIX: {live_vix:.2f})! Base target {base_target_pct*100:.2f}% hit. "
                        f"Spot is safe ({spot_distance*100:.2f}% away). "
                        f"RATCHET TRAIL ACTIVATED — Floor set at {initial_floor*100:.2f}%, hunting for more!"
                    )
                else:
                    logging.critical(
                        f"🎯 TARGET REACHED outside safe zone (Distance: {spot_distance*100:.2f}% > "
                        f"Limit: {dynamic_zone_tolerance*100:.2f}%). Squaring off for guaranteed "
                        f"{base_target_pct*100:.2f}% profit."
                    )
                    return "TAKE_PROFIT", current_prices
            else:
                logging.critical(
                    f"🎯 TARGET REACHED! (Spot data unavailable). "
                    f"Squaring off for guaranteed {base_target_pct*100:.2f}% profit."
                )
                return "TAKE_PROFIT", current_prices

    # ================================================================
    # CHECK 3: STOP LOSS — FREAK TICK FILTER (3-tick confirmation)
    # ================================================================
    entry_sell_ce = float(entries['sell_ce'])
    entry_sell_pe = float(entries['sell_pe'])

    limit_ce = entry_sell_ce * 2.0
    limit_pe = entry_sell_pe * 2.0
    
    sl_breach_count = state.get("sl_breach_count", 0)

    if float(live_sell_ce) >= limit_ce or float(live_sell_pe) >= limit_pe:
        sl_breach_count += 1
        state_manager.update_state("sl_breach_count", sl_breach_count)
        
        logging.warning(f"⚠️ FREAK TICK WARNING: Stop Loss breached. Confirmation count: {sl_breach_count}/3")
        
        if sl_breach_count >= 3:
            logging.critical("🚨 CONFIRMED STOP LOSS: Price sustained above limit for 3 ticks. Exiting.")
            return "STOP_LOSS", current_prices
        return False, {} 
        
    else:
        if sl_breach_count > 0:
            state_manager.update_state("sl_breach_count", 0)
            
    return False, {}
