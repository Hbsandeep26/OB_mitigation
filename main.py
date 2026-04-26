# main.py
import os
import sys
import logging
import json
import re
import schedule
import time
from datetime import datetime, timedelta
import config
import state_manager

# Note the new imports: get_fresh_option_quotes, get_india_vix, get_spot_with_ohlc
from data_feed import get_spot_price, get_option_chain, monitor_live_prices, get_fresh_option_quotes, get_india_vix, get_spot_with_ohlc
from strategy import calculate_iron_butterfly_legs, risk_management_evaluator, get_vix_session_profile
from execution import place_iron_butterfly_basket, square_off_all

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ============================================================================
# THE DOUBLE-LOG FIX: Only add StreamHandler when running in a real terminal.
# When launched via dashboard's subprocess.Popen(stdout=log_file), stdout IS
# bot.log, so StreamHandler + RotatingFileHandler both write to bot.log = dupes.
# ============================================================================
from logging.handlers import RotatingFileHandler
logger = logging.getLogger()
logger.setLevel(logging.INFO)
logger.propagate = False

if logger.hasHandlers():
    logger.handlers.clear()

log_file_path = os.path.join(BASE_DIR, "bot.log")
file_handler = RotatingFileHandler(log_file_path, maxBytes=5*1024*1024, backupCount=3)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# Only add console output when running in a real terminal (not piped to file)
if sys.stdout.isatty():
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)


# ============================================================================
# EXPIRY DATE VALIDATOR
# ============================================================================

def is_valid_expiry(expiry_date):
    """Validates that expiry_date is in YYYY-MM-DD format and not a placeholder."""
    if not expiry_date or expiry_date in ("UNKNOWN", "RECOVERY", ""):
        return False
    return bool(re.match(r'^\d{4}-\d{2}-\d{2}$', expiry_date))


# ============================================================================
# OPENING RANGE GAP FILTER
# ============================================================================

def check_opening_gap(index_symbol):
    """
    Checks if the market has gapped more than 0.8% from previous close.
    Returns (gap_detected: bool, gap_pct: float).
    If gap is detected, the bot should pause for 15 minutes.
    """
    try:
        ltp, prev_close = get_spot_with_ohlc(index_symbol)
        
        if ltp and prev_close and prev_close > 0:
            gap_pct = abs(ltp - prev_close) / prev_close
            gap_direction = "UP" if ltp > prev_close else "DOWN"
            
            logging.info(
                f"📊 Gap Analysis: {index_symbol} opened at {ltp:.2f}, "
                f"Prev Close: {prev_close:.2f}, Gap: {gap_pct*100:.2f}% {gap_direction}"
            )
            
            if gap_pct > config.GAP_THRESHOLD_PCT:
                logging.warning(
                    f"⚠️ OPENING GAP DETECTED! {index_symbol} gapped {gap_direction} "
                    f"{gap_pct*100:.2f}% (threshold: {config.GAP_THRESHOLD_PCT*100:.1f}%). "
                    f"Pausing for {config.GAP_SETTLE_MINUTES} minutes to let volatility absorb."
                )
                return True, gap_pct
            else:
                logging.info(f"✅ Gap within tolerance ({gap_pct*100:.2f}% < {config.GAP_THRESHOLD_PCT*100:.1f}%). Proceeding normally.")
                return False, gap_pct
        else:
            logging.warning("⚠️ Could not fetch OHLC data for gap analysis. Skipping gap filter.")
            return False, 0.0
    except Exception as e:
        logging.error(f"Gap filter error: {e}. Skipping gap check.")
        return False, 0.0


# ============================================================================
# CONTINUOUS TRADING SESSION
# ============================================================================

def continuous_trading_session(index_symbol, expiry_date, cutoff_hour, cutoff_minute):
    logging.info(f"--- STARTING CONTINUOUS SESSION FOR {index_symbol} ---")

    # ================================================================
    # EXPIRY DATE VALIDATION GUARD
    # ================================================================
    if not is_valid_expiry(expiry_date):
        logging.critical(
            f"❌ INVALID EXPIRY DATE: '{expiry_date}' for {index_symbol}. "
            f"Cannot deploy trades. Please fix the expiry in settings.json or the dashboard!"
        )
        import notifier
        notifier.send_telegram_alert(
            f"❌ <b>INVALID EXPIRY DATE!</b>\n"
            f"{index_symbol}: '{expiry_date}'\n"
            f"Fix settings.json and restart."
        )
        return

    manual_exit_file = os.path.join(BASE_DIR, "manual_exit_flag.txt")
    if os.path.exists(manual_exit_file):
        os.remove(manual_exit_file)
        
    # ================================================================
    # PHASE 0: BTST CARRY FORWARD RECOVERY
    # ================================================================
    btst_exit_reason = None  # Track why the carry-forward exited
    
    state = state_manager.load_state()
    if state and state.get("active"):
        logging.critical(f"🌙 BTST CARRY FORWARD DETECTED: Waking up existing {index_symbol} trade.")
        stop_loss_hit, exit_prices = monitor_live_prices(state['legs'], risk_management_evaluator)
        
        if stop_loss_hit:
            btst_exit_reason = stop_loss_hit  # Remember why we exited
            
            if stop_loss_hit == "TAKE_PROFIT":
                logging.critical(f"💰 PROFIT LOCKED on {index_symbol} Carry Forward trade! Squaring off...")
            elif stop_loss_hit == "STOP_LOSS":
                logging.warning(f"🚨 Stop Loss hit on {index_symbol} Carry Forward trade! Squaring off...")
            elif stop_loss_hit == "ATM_DRIFT":
                logging.critical(f"🌊 ATM Drift detected on {index_symbol} Carry Forward. Structure broken — squaring off...")
            elif stop_loss_hit == "TIME_EXIT":
                logging.critical(f"⏰ EOD Cutoff triggered on Carry Forward trade. Squaring off...")
            elif stop_loss_hit == "MANUAL_EXIT":
                logging.critical(f"🛑 Manual Exit on {index_symbol} Carry Forward. Squaring off...")
            else:
                logging.info(f"🔄 Exit Signal ({stop_loss_hit}) received on Carry Forward. Squaring off...")
                
            square_off_all(exit_prices)
            time.sleep(10)
            
        btst_file = os.path.join(BASE_DIR, "btst_flag.txt")
        if os.path.exists(btst_file):
            try:
                os.remove(btst_file)
                logging.info("🧹 Auto-cleared BTST flag from UI so the fresh trade doesn't carry forward blindly.")
            except Exception: pass

        # --- BUG FIX: If manual exit was triggered, STOP the session entirely ---
        if btst_exit_reason == "MANUAL_EXIT":
            logging.critical("⏸️ MANUAL EXIT on carry-forward — halting session. No new trades will be deployed.")
            return

    # ================================================================
    # PHASE 1: OPENING RANGE GAP FILTER
    # ================================================================
    gap_detected, gap_pct = check_opening_gap(index_symbol)
    if gap_detected:
        # --- BUG FIX: Use timedelta instead of manual minute arithmetic (overflow-safe) ---
        settle_end = datetime.now() + timedelta(minutes=config.GAP_SETTLE_MINUTES)
        
        logging.info(f"⏳ Gap Filter: Waiting until {settle_end.strftime('%H:%M')} for market to stabilize...")
        while datetime.now() < settle_end:
            now = datetime.now()
            if now.hour > cutoff_hour or (now.hour == cutoff_hour and now.minute >= cutoff_minute):
                logging.info(f"⏰ Cutoff reached during gap wait. Ending session.")
                return
            
            # Check for manual exit to avoid being deaf for 15 minutes
            manual_exit_file = os.path.join(BASE_DIR, "manual_exit_flag.txt")
            if os.path.exists(manual_exit_file):
                os.remove(manual_exit_file)
                logging.critical("🛑 MANUAL EXIT triggered during Gap Filter wait. Halting session.")
                return
                
            time.sleep(5)
        logging.info("✅ Gap settle period complete. Resuming normal operations.")

    # ================================================================
    # PHASE 2: SAFE ENTRY WINDOW WAIT
    # ================================================================
    if cutoff_hour == 12: 
        target_entry_time = datetime.strptime("09:20", "%H:%M").time()
        logged_wait = False
        while datetime.now().time() < target_entry_time:
            if not logged_wait:
                logging.info("⏳ Waiting for 09:20 AM safe entry window before deploying fresh trade...")
                logged_wait = True
            time.sleep(2)

    # ================================================================
    # PHASE 3: MAIN TRADING LOOP (with Circuit Breaker + API Retry Limit)
    # ================================================================
    consecutive_losses = 0
    api_retry_count = 0
    MAX_API_RETRIES = 5

    while True:
        now = datetime.now()
        
        if now.hour > cutoff_hour or (now.hour == cutoff_hour and now.minute >= cutoff_minute):
            logging.info(f"⏰ Soft Cutoff ({cutoff_hour}:{cutoff_minute:02d}) reached. No NEW {index_symbol} trades will be taken.")
            break 

        # --- CIRCUIT BREAKER CHECK ---
        if consecutive_losses >= config.MAX_CONSECUTIVE_LOSSES:
            logging.critical(
                f"🔌 CIRCUIT BREAKER TRIPPED! {consecutive_losses} consecutive losses detected. "
                f"Halting ALL new trades for {index_symbol} this session."
            )
            import notifier
            notifier.send_telegram_alert(
                f"🔌 <b>CIRCUIT BREAKER ACTIVATED!</b>\n"
                f"{index_symbol}: {consecutive_losses} consecutive losses.\n"
                f"Bot has halted trading for this session."
            )
            break

        logging.info(f"Deploying fresh Iron Butterfly for {index_symbol}...")

        # --- FETCH SPOT PRICE ---
        spot = get_spot_price(index_symbol)
        chain = get_option_chain(index_symbol, expiry_date) if spot else None
        
        if not spot or not chain:
            api_retry_count += 1
            if api_retry_count >= MAX_API_RETRIES:
                logging.critical(
                    f"❌ API failures exceeded {MAX_API_RETRIES} retries for {index_symbol}. "
                    f"Halting session to prevent infinite loop. Check Upstox token and expiry date ({expiry_date})."
                )
                import notifier
                notifier.send_telegram_alert(
                    f"❌ <b>API FAILURE LIMIT HIT!</b>\n"
                    f"{index_symbol}: {MAX_API_RETRIES} consecutive API failures.\n"
                    f"Expiry: {expiry_date}\n"
                    f"Check token and settings!"
                )
                break
            logging.warning(f"⚠️ API Rejection ({api_retry_count}/{MAX_API_RETRIES}): Spot found={bool(spot)}, Chain found={bool(chain)}. Check Upstox Token or Expiry Date ({expiry_date}). Retrying in 30s...")
            time.sleep(30)
            continue
        
        # Reset API retry counter on success
        api_retry_count = 0

        # --- FETCH INDIA VIX & DETERMINE SESSION PROFILE ---
        live_vix = get_india_vix()
        if live_vix is None:
            live_vix = 15.0  # Safe default if VIX API fails
            logging.warning(f"⚠️ VIX fetch failed. Using default VIX={live_vix}")
        
        session_profile = get_vix_session_profile(live_vix)

        # --- CALCULATE IRON BUTTERFLY WITH DELTA-BASED WINGS ---
        legs, entry_prices, strikes = calculate_iron_butterfly_legs(
            index_symbol, spot, chain, wing_delta=session_profile['wing_delta']
        )
        
        if not legs:
            api_retry_count += 1
            if api_retry_count >= MAX_API_RETRIES:
                logging.critical(f"❌ Wing calculation failed {MAX_API_RETRIES} times. Halting session.")
                break
            logging.warning(f"⚠️ Math Rejection ({api_retry_count}/{MAX_API_RETRIES}): Could not find valid protective wings for {index_symbol} in the current option chain. Retrying in 30s...")
            time.sleep(30)
            continue
        
        # Reset API retry counter on success
        api_retry_count = 0
        
        # --- THE PRICE SYNCHRONIZATION FIX ---
        logging.info("Synchronizing with Live Exchange Quotes to bypass cached API data...")
        fresh_quotes = get_fresh_option_quotes(list(legs.values()))
        if fresh_quotes:
            for leg_name, token in legs.items():
                if token in fresh_quotes and fresh_quotes[token] > 0:
                    entry_prices[leg_name] = fresh_quotes[token]
            logging.info(f"Absolute Real-Time Entry Prices: {entry_prices}")
        else:
            logging.warning("Sync failed. Falling back to option chain prices.")
            
        execution_success = place_iron_butterfly_basket(legs, index_symbol, entry_prices, strikes)
        
        if not execution_success:
            logging.critical("🛑 CRITICAL: Basket execution failed mid-flight! Halting session to prevent orphan legs.")
            import notifier
            notifier.send_telegram_alert(f"🚨 <b>URGENT ACTION REQUIRED!</b> 🚨\n{index_symbol} basket order failed mid-execution. Check Upstox App immediately.")
            break 

        # --- INJECT VIX PROFILE INTO TRADE STATE (Dashboard Sync) ---
        state_manager.update_state("applied_target_pct", session_profile['target_pct'])
        state_manager.update_state("vix_profile", session_profile['name'])
        state_manager.update_state("session_vix", round(live_vix, 2))
        state_manager.update_state("profit_high_water_mark", 0.0)
        state_manager.update_state("trail_active", False)
        state_manager.update_state("trail_floor", 0.0)
        state_manager.update_state("atm_drift_ratio", 0.0)
            
        state_manager.update_state("cutoff_hour", cutoff_hour)
        state_manager.update_state("cutoff_minute", cutoff_minute)
            
        logging.info("Entering live risk monitoring phase...")
        trade_entry_time = datetime.now()  # Track when this trade was deployed

        stop_loss_hit, exit_prices = monitor_live_prices(legs, risk_management_evaluator)

        # ================================================================
        # EXIT SIGNAL HANDLING
        # ================================================================
        
        # --- MINIMUM TRADE DURATION GUARD ---
        # If a trade exits within 30 seconds of entry, something is wrong
        # (stale prices, instant stop-loss on first tick, etc.)
        trade_duration = (datetime.now() - trade_entry_time).total_seconds()
        if trade_duration < 30 and stop_loss_hit not in ("MANUAL_EXIT", "TIME_EXIT", "MARKET_CLOSED"):
            if stop_loss_hit == "STOP_LOSS":
                logging.critical(f"⚠️ GENUINE FLASH CRASH! Trade hit STOP LOSS in just {trade_duration:.0f}s. Squaring off and pausing for 120 seconds.")
            else:
                logging.critical(
                    f"⚠️ FLASH EXIT DETECTED! Trade lasted only {trade_duration:.0f}s (Reason: {stop_loss_hit}). "
                    f"This likely means stale/incomplete price data triggered a false exit. "
                    f"Squaring off safely and pausing for 120 seconds."
                )
            square_off_all(exit_prices)
            consecutive_losses += 1
            time.sleep(120)
            continue

        if stop_loss_hit == "MANUAL_EXIT":
            logging.critical(f"🛑 Manual Exit executed. Squaring off {index_symbol}...")
            square_off_all(exit_prices)
            logging.critical("⏸️ Trading paused for this session due to Manual Exit. Restart the bot engine if you wish to force a re-entry.")
            break 

        elif stop_loss_hit == "TAKE_PROFIT":
            logging.critical(f"💰 PROFIT LOCKED for {index_symbol}! Squaring off positions.")
            square_off_all(exit_prices)
            consecutive_losses = 0  # Reset circuit breaker on a win
            logging.info("Cooling down for 60 seconds before looking for new setups...")
            time.sleep(60)

        elif stop_loss_hit == "STOP_LOSS":
            logging.warning(f"🚨 Stop Loss hit for {index_symbol}! Squaring off...")
            square_off_all(exit_prices)
            consecutive_losses += 1
            logging.info(f"🔌 Circuit Breaker: {consecutive_losses}/{config.MAX_CONSECUTIVE_LOSSES} consecutive losses.")
            logging.info("Cooling down for 60 seconds...")
            time.sleep(60)

        elif stop_loss_hit == "ATM_DRIFT":
            logging.critical(f"🌊 ATM Drift exit for {index_symbol}! Structure compromised — squaring off...")
            square_off_all(exit_prices)
            consecutive_losses += 1
            logging.info(f"🔌 Circuit Breaker: {consecutive_losses}/{config.MAX_CONSECUTIVE_LOSSES} consecutive losses.")
            logging.info("Cooling down for 60 seconds...")
            time.sleep(60)
        
        elif stop_loss_hit == "MARKET_CLOSED":
            logging.info(f"🛑 Live market feed ended for {index_symbol}. Proceeding to EOD evaluation.")
            break

        elif stop_loss_hit == "SOCKET_DEAD":
            # --- SOCKET_DEAD: WebSocket died, NOT a trading loss ---
            logging.critical(f"☠️ WebSocket died for {index_symbol}. This is NOT a trading loss — attempting reconnection...")
            # Don't count as a loss, don't square off — let the loop retry with a fresh connection
            logging.info("Cooling down for 30 seconds before reconnection attempt...")
            time.sleep(30)
            continue  # Skip the rest, retry from the top

        elif stop_loss_hit == "TIME_EXIT":
            logging.critical(f"⏰ EOD Cutoff triggered for {index_symbol}. Squaring off and ending session.")
            square_off_all(exit_prices)
            break 

        elif stop_loss_hit:
            logging.warning(f"Stop Loss hit for {index_symbol}! Squaring off...")
            square_off_all(exit_prices)
            consecutive_losses += 1
            logging.info("Cooling down for 60 seconds...")
            time.sleep(60)
        else:
            logging.error("WebSocket connection terminated unexpectedly.")
            break
        
    logging.info(f"--- END OF SESSION FOR {index_symbol} ---")
    
    btst_file = os.path.join(BASE_DIR, "btst_flag.txt")
    if os.path.exists(btst_file) and open(btst_file, "r").read().strip() == "TRUE":
        if state_manager.load_state():
            logging.critical(f"🌙 BTST ENABLED: Carrying forward {index_symbol} overnight.")
            return 

    final_state = state_manager.load_state()
    if final_state and final_state.get("active"):
        logging.info(f"Initiating final safety square off for {index_symbol}...")
        exit_prices = None
        live_file = os.path.join(BASE_DIR, "live_prices.json")
        
        if os.path.exists(live_file):
            latest_ticks = {} 
            try:
                with open(live_file, "r") as f:
                    latest_ticks = json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                pass
            
            if latest_ticks: 
                legs = final_state['legs']
                try:
                    exit_prices = {
                        'sell_ce': latest_ticks.get(legs['sell_ce'], {}).get('ltp', 0),
                        'sell_pe': latest_ticks.get(legs['sell_pe'], {}).get('ltp', 0),
                        'buy_ce': latest_ticks.get(legs['buy_ce'], {}).get('ltp', 0),
                        'buy_pe': latest_ticks.get(legs['buy_pe'], {}).get('ltp', 0)
                    }
                except AttributeError: pass 
                    
        square_off_all(exit_prices)


# ============================================================================
# SCHEDULE BUILDER — EXPIRY DAY STRATEGY
# ============================================================================

def build_todays_schedule():
    schedule.clear('trading_jobs')
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    # ================================================================
    # WEEKEND GUARD: Never create trading jobs on Saturday/Sunday
    # ================================================================
    if now.weekday() >= 5:
        logging.info(f"📅 WEEKEND DETECTED ({today_str} - {now.strftime('%A')}). No trading jobs scheduled.")
        return

    if hasattr(config, 'MARKET_HOLIDAYS') and today_str in config.MARKET_HOLIDAYS:
        logging.critical(f"🌴 MARKET HOLIDAY DETECTED ({today_str}). The bot will sleep all day.")
        return 

    nifty_expiry, sensex_expiry = "UNKNOWN", "UNKNOWN"
    settings_file = os.path.join(BASE_DIR, "settings.json")
    if os.path.exists(settings_file):
        try:
            with open(settings_file, "r") as f:
                data = json.load(f)
                nifty_expiry = data.get("NIFTY_EXPIRY", "UNKNOWN")
                sensex_expiry = data.get("SENSEX_EXPIRY", "UNKNOWN")
        except Exception: pass

    # ================================================================
    # EXPIRY DAY STRATEGY:
    # The EXPIRING index trades in the MORNING (first half) to capture
    # rapid theta decay. The other index gets the afternoon slot.
    # ================================================================

    if today_str == nifty_expiry:
        logging.critical(f"🎯 NIFTY EXPIRY DETECTED ({today_str}). Expiry Strategy: Nifty MORNING (theta capture), Sensex AFTERNOON.")
        # Nifty in the morning (expiring — capture rapid theta decay)
        schedule.every().day.at("09:15").do(
            continuous_trading_session, index_symbol="NIFTY", 
            expiry_date=nifty_expiry, cutoff_hour=12, cutoff_minute=30
        ).tag('trading_jobs')
        # Sensex in the afternoon (safe, not expiring)
        schedule.every().day.at("12:31").do(
            continuous_trading_session, index_symbol="SENSEX", 
            expiry_date=sensex_expiry, cutoff_hour=15, cutoff_minute=15
        ).tag('trading_jobs')

    elif today_str == sensex_expiry:
        logging.critical(f"🎯 SENSEX EXPIRY DETECTED ({today_str}). Expiry Strategy: Sensex MORNING (theta capture), Nifty AFTERNOON.")
        # Sensex in the morning (expiring — capture rapid theta decay)
        schedule.every().day.at("09:15").do(
            continuous_trading_session, index_symbol="SENSEX", 
            expiry_date=sensex_expiry, cutoff_hour=12, cutoff_minute=30
        ).tag('trading_jobs')
        # Nifty in the afternoon (safe, not expiring)
        schedule.every().day.at("12:31").do(
            continuous_trading_session, index_symbol="NIFTY", 
            expiry_date=nifty_expiry, cutoff_hour=15, cutoff_minute=15
        ).tag('trading_jobs')
    
    else:
        weekday = now.strftime("%A").upper()
        if weekday in ["WEDNESDAY", "THURSDAY"]:
            logging.info(f"📅 Normal Trading Day ({today_str} - {weekday}). Defaulting to SENSEX.")
            schedule.every().day.at("09:15").do(continuous_trading_session, index_symbol="SENSEX", expiry_date=sensex_expiry, cutoff_hour=15, cutoff_minute=15).tag('trading_jobs')
        else:
            logging.info(f"📅 Normal Trading Day ({today_str} - {weekday}). Defaulting to NIFTY.")
            schedule.every().day.at("09:15").do(continuous_trading_session, index_symbol="NIFTY", expiry_date=nifty_expiry, cutoff_hour=15, cutoff_minute=15).tag('trading_jobs')


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    # Handler setup already done at module level — no need to duplicate here
        
    logging.info("System Architect Bot V5 Initialized — Master Architecture Active 🏛️")
    logging.info(f"  VIX Adaptive Profiles: ON (Low<{config.VIX_LOW_THRESHOLD}, High>{config.VIX_HIGH_THRESHOLD})")
    logging.info(f"  Delta-Based Wings: ON")
    logging.info(f"  Ratchet Trailing Stop: ON (Floor={config.TRAIL_LOCK_FLOOR_PCT*100:.0f}%, Ratchet={config.TRAIL_RATCHET_FACTOR*100:.0f}%)")
    logging.info(f"  ATM Drift Guard: ON ({config.ATM_DRIFT_MULTIPLIER}x wing width)")
    logging.info(f"  Circuit Breaker: ON ({config.MAX_CONSECUTIVE_LOSSES} consecutive losses)")
    logging.info(f"  Opening Range Gap Filter: ON ({config.GAP_THRESHOLD_PCT*100:.1f}% threshold, {config.GAP_SETTLE_MINUTES}min settle)")
    logging.info(f"  Expiry Day Strategy: Expiring index trades MORNING, other AFTERNOON")
    logging.info(f"  Weekend Guard: ON")

    nifty_expiry, sensex_expiry = "UNKNOWN", "UNKNOWN"
    settings_file = os.path.join(BASE_DIR, "settings.json")
    if os.path.exists(settings_file):
        try:
            with open(settings_file, "r") as f:
                data = json.load(f)
                nifty_expiry, sensex_expiry = data.get("NIFTY_EXPIRY", "UNKNOWN"), data.get("SENSEX_EXPIRY", "UNKNOWN")
        except Exception: pass

    build_todays_schedule()
    schedule.every().day.at("08:00").do(build_todays_schedule)

    recovered_state = state_manager.load_state()

    if recovered_state and recovered_state.get("active"):
        rec_index = recovered_state.get("index_symbol", "UNKNOWN")
        rec_expiry = nifty_expiry if rec_index == "NIFTY" else sensex_expiry
        
        logging.critical(f"🔄 ORPHANED TRADE DETECTED ON BOOT! Instantly recovering {rec_index} session...")

        continuous_trading_session(
            index_symbol=rec_index,
            expiry_date=rec_expiry, 
            cutoff_hour=15, 
            cutoff_minute=15
        )
    
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    
    if not (hasattr(config, 'MARKET_HOLIDAYS') and today_str in config.MARKET_HOLIDAYS) and now.weekday() < 5:
        current_time = now.time()
        morning_start = datetime.strptime("09:15", "%H:%M").time() 
        afternoon_start = datetime.strptime("12:31", "%H:%M").time()
        eod_cutoff = datetime.strptime("15:15", "%H:%M").time()

        # --- EXPIRY DAY STRATEGY: Expiring index trades MORNING, other AFTERNOON ---
        if today_str == nifty_expiry:
            # Nifty expiring → Nifty morning (theta capture), Sensex afternoon
            morning_idx, afternoon_idx = "NIFTY", "SENSEX"
            morning_exp, afternoon_exp = nifty_expiry, sensex_expiry
        elif today_str == sensex_expiry:
            # Sensex expiring → Sensex morning (theta capture), Nifty afternoon
            morning_idx, afternoon_idx = "SENSEX", "NIFTY"
            morning_exp, afternoon_exp = sensex_expiry, nifty_expiry
        else:
            default_idx = "SENSEX" if now.strftime("%A").upper() in ["WEDNESDAY", "THURSDAY"] else "NIFTY"
            default_exp = sensex_expiry if default_idx == "SENSEX" else nifty_expiry
            morning_idx, afternoon_idx = default_idx, default_idx
            morning_exp, afternoon_exp = default_exp, default_exp

        if morning_start <= current_time < afternoon_start:
            logging.critical(f"🏃 LATE BOOT DETECTED! Jumping straight into Morning Session ({morning_idx})...")
            continuous_trading_session(morning_idx, morning_exp, 12, 30)
            
        elif afternoon_start <= current_time < eod_cutoff:
            logging.critical(f"🏃 LATE BOOT DETECTED! Jumping straight into Afternoon Session ({afternoon_idx})...")
            continuous_trading_session(afternoon_idx, afternoon_exp, 15, 15)

    logging.info("Waiting for scheduled events...")
    while True:
        schedule.run_pending()
        time.sleep(1)
