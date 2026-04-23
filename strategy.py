# strategy.py
import config
import logging
import state_manager
import time
import os
import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def calculate_iron_butterfly_legs(index_symbol, spot_price, option_chain_data):
    logging.info("Calculating Iron Butterfly strikes & prices...")
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
                best_ce_diff, buy_ce_key, buy_ce_ltp = abs(ce_ltp - target_ce_buy), call_info.get('instrument_key'), ce_ltp
                buy_ce_strike = strike

        if strike < atm_strike and put_info:
            pe_ltp = put_info.get('market_data', {}).get('ltp', 0)
            if pe_ltp > 0 and abs(pe_ltp - target_pe_buy) < best_pe_diff:
                best_pe_diff, buy_pe_key, buy_pe_ltp = abs(pe_ltp - target_pe_buy), put_info.get('instrument_key'), pe_ltp
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

    manual_exit_file = os.path.join(BASE_DIR, "manual_exit_flag.txt")
    if os.path.exists(manual_exit_file):
        with open(manual_exit_file, "r") as f:
            if f.read().strip() == "TRUE":
                logging.critical("🛑 MANUAL EXIT TRIGGERED FROM UI! Forcing Square Off.")
                os.remove(manual_exit_file)
                return "MANUAL_EXIT", current_prices 

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

    entry_net = (entries['sell_ce'] + entries['sell_pe']) - (entries['buy_ce'] + entries['buy_pe'])
    live_net = (live_sell_ce + live_sell_pe) - (live_buy_ce + live_buy_pe)
    
    base_target_pct = config.get_target_profit_pct() / 100.0
    TRAIL_STEP_PCT = base_target_pct * 0.50      
    LOCK_IN_PCT = base_target_pct * 0.80         

    trail_active = state.get("trail_active", False)

    if trail_active:
        greedy_target_pct = base_target_pct + TRAIL_STEP_PCT
        
        greedy_exit_premium = entry_net * (1.0 - greedy_target_pct)
        lock_in_exit_premium = entry_net * (1.0 - LOCK_IN_PCT)
        
        if live_net <= greedy_exit_premium:
            logging.critical(f"🌟 MAX TRAIL REACHED! Premium decayed to ₹{live_net:.2f}. Locking in massive {greedy_target_pct*100:.2f}% profit.")
            return "TAKE_PROFIT", current_prices
            
        elif live_net >= lock_in_exit_premium:
            logging.critical(f"🛡️ TRAIL STOP HIT! Market reversed, but we locked in {LOCK_IN_PCT*100:.2f}% guaranteed profit.")
            return "TAKE_PROFIT", current_prices

    else:
        target_exit_premium = entry_net * (1.0 - base_target_pct)
        
        if live_net <= target_exit_premium:
            index_symbol = state.get('index_symbol', 'NIFTY')
            spot_key = "NSE_INDEX|Nifty 50" if index_symbol == "NIFTY" else "BSE_INDEX|SENSEX"
            vix_key = "NSE_INDEX|India VIX"
            
            live_spot = live_data.get(spot_key, {}).get('ltp', 0.0)
            live_vix = live_data.get(vix_key, {}).get('ltp', 15.0) 
            atm_strike = state.get('strikes', {}).get('sell_ce', 0.0)
            
            if live_spot > 0 and atm_strike > 0:
                spot_distance = abs(live_spot - atm_strike) / atm_strike
                
                daily_expected_move = live_vix / 19.1
                dynamic_zone_tolerance = (daily_expected_move / 100.0) * 0.20 
                
                if spot_distance <= dynamic_zone_tolerance:
                    logging.critical(f"🚨 ZONE DEFENSE (VIX: {live_vix:.2f})! Target {base_target_pct*100:.2f}% hit. Spot is safe ({spot_distance*100:.2f}% away). Upgrading target to {(base_target_pct + TRAIL_STEP_PCT)*100:.2f}%!")
                    state_manager.update_state("trail_active", True)
                else:
                    logging.critical(f"🎯 TARGET REACHED outside safe zone (Distance: {spot_distance*100:.2f}% > Limit: {dynamic_zone_tolerance*100:.2f}%). Squaring off for guaranteed {base_target_pct*100:.2f}% profit.")
                    return "TAKE_PROFIT", current_prices
            else:
                logging.critical(f"🎯 TARGET REACHED! (Spot data unavailable). Squaring off for guaranteed {base_target_pct*100:.2f}% profit.")
                return "TAKE_PROFIT", current_prices

    # --- THE ORIGINAL ROBUST FREAK TICK FILTER ---
    # This requires 3 ticks of confirmation, saving you from WebSocket anomalies.
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
