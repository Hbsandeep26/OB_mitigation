# main.py
import os
import logging
import json
import schedule
import time
from datetime import datetime
import config
import state_manager

# Note the new import: get_fresh_option_quotes
from data_feed import get_spot_price, get_option_chain, monitor_live_prices, get_fresh_option_quotes
from strategy import calculate_iron_butterfly_legs, risk_management_evaluator
from execution import place_iron_butterfly_basket, square_off_all

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# --- THE LOGGING FIX: Prevent Double Logs ---
from logging.handlers import RotatingFileHandler
logger = logging.getLogger()
logger.setLevel(logging.INFO)
logger.propagate = False # THIS FIXES THE DOUBLE PRINTING

if logger.hasHandlers():
    logger.handlers.clear()

log_file_path = os.path.join(BASE_DIR, "bot.log")
file_handler = RotatingFileHandler(log_file_path, maxBytes=5*1024*1024, backupCount=3)
stream_handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
stream_handler.setFormatter(formatter)
logger.addHandler(file_handler)
logger.addHandler(stream_handler)

def continuous_trading_session(index_symbol, expiry_date, cutoff_hour, cutoff_minute):
    logging.info(f"--- STARTING CONTINUOUS SESSION FOR {index_symbol} ---")

    manual_exit_file = os.path.join(BASE_DIR, "manual_exit_flag.txt")
    if os.path.exists(manual_exit_file):
        os.remove(manual_exit_file)
        
    state = state_manager.load_state()
    if state and state.get("active"):
        logging.critical(f"🌙 BTST CARRY FORWARD DETECTED: Waking up existing {index_symbol} trade.")
        stop_loss_hit, exit_prices = monitor_live_prices(state['legs'], risk_management_evaluator)
        
        if stop_loss_hit:
            if stop_loss_hit == "TAKE_PROFIT":
                logging.critical(f"💰 PROFIT LOCKED on {index_symbol} Carry Forward trade! Squaring off...")
            elif stop_loss_hit == "STOP_LOSS":
                logging.warning(f"🚨 Stop Loss hit on {index_symbol} Carry Forward trade! Squaring off...")
            elif stop_loss_hit == "TIME_EXIT":
                logging.critical(f"⏰ EOD Cutoff triggered on Carry Forward trade. Squaring off...")
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

    if cutoff_hour == 12: 
        target_entry_time = datetime.strptime("09:20", "%H:%M").time()
        logged_wait = False
        while datetime.now().time() < target_entry_time:
            if not logged_wait:
                logging.info("⏳ Waiting for 09:20 AM safe entry window before deploying fresh trade...")
                logged_wait = True
            time.sleep(2)

    while True:
        now = datetime.now()
        
        if now.hour > cutoff_hour or (now.hour == cutoff_hour and now.minute >= cutoff_minute):
            logging.info(f"⏰ Soft Cutoff ({cutoff_hour}:{cutoff_minute}) reached. No NEW {index_symbol} trades will be taken.")
            break 

        logging.info(f"Deploying fresh Iron Butterfly for {index_symbol}...")

        spot = get_spot_price(index_symbol)
        chain = get_option_chain(index_symbol, expiry_date) if spot else None
        
        if not spot or not chain:
            logging.warning(f"⚠️ API Rejection: Spot found={bool(spot)}, Chain found={bool(chain)}. Check Upstox Token or Expiry Date ({expiry_date}). Retrying in 30s...")
            time.sleep(30)
            continue

        legs, entry_prices, strikes = calculate_iron_butterfly_legs(index_symbol, spot, chain)
        if not legs:
            logging.warning(f"⚠️ Math Rejection: Could not find valid protective wings for {index_symbol} in the current option chain. Retrying in 30s...")
            time.sleep(30)
            continue
        
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
            
        state_manager.update_state("cutoff_hour", cutoff_hour)
        state_manager.update_state("cutoff_minute", cutoff_minute)
            
        logging.info("Entering live risk monitoring phase...")

        stop_loss_hit, exit_prices = monitor_live_prices(legs, risk_management_evaluator)

        if stop_loss_hit == "MANUAL_EXIT":
            logging.critical(f"🛑 Manual Exit executed. Squaring off {index_symbol}...")
            square_off_all(exit_prices)
            logging.critical("⏸️ Trading paused for this session due to Manual Exit. Restart the bot engine if you wish to force a re-entry.")
            break 

        elif stop_loss_hit == "TAKE_PROFIT":
            logging.critical(f"💰 PROFIT LOCKED for {index_symbol}! Squaring off positions.")
            square_off_all(exit_prices)
            logging.info("Cooling down for 60 seconds before looking for new setups...")
            time.sleep(60)

        elif stop_loss_hit == "STOP_LOSS":
            logging.warning(f"🚨 Stop Loss hit for {index_symbol}! Squaring off...")
            square_off_all(exit_prices)
            logging.info("Cooling down for 60 seconds...")
            time.sleep(60)
        
        elif stop_loss_hit == "MARKET_CLOSED":
            logging.info(f"🛑 Live market feed ended for {index_symbol}. Proceeding to EOD evaluation.")
            break 

        elif stop_loss_hit == "TIME_EXIT":
            logging.critical(f"⏰ EOD Cutoff triggered for {index_symbol}. Squaring off and ending session.")
            square_off_all(exit_prices)
            break 

        elif stop_loss_hit:
            logging.warning(f"Stop Loss hit for {index_symbol}! Squaring off...")
            square_off_all(exit_prices)
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

def build_todays_schedule():
    schedule.clear('trading_jobs')
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

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

    if today_str == nifty_expiry:
        logging.critical(f"🎯 NIFTY EXPIRY DETECTED ({today_str}). Loading Nifty Relay.")
        schedule.every().day.at("09:15").do(continuous_trading_session, index_symbol="NIFTY", expiry_date=nifty_expiry, cutoff_hour=12, cutoff_minute=30).tag('trading_jobs')
        schedule.every().day.at("12:31").do(continuous_trading_session, index_symbol="SENSEX", expiry_date=sensex_expiry, cutoff_hour=15, cutoff_minute=15).tag('trading_jobs')

    elif today_str == sensex_expiry:
        logging.critical(f"🎯 SENSEX EXPIRY DETECTED ({today_str}). Loading Sensex Relay.")
        schedule.every().day.at("09:15").do(continuous_trading_session, index_symbol="SENSEX", expiry_date=sensex_expiry, cutoff_hour=12, cutoff_minute=30).tag('trading_jobs')
        schedule.every().day.at("12:31").do(continuous_trading_session, index_symbol="NIFTY", expiry_date=nifty_expiry, cutoff_hour=15, cutoff_minute=15).tag('trading_jobs')
    
    else:
        weekday = now.strftime("%A").upper()
        if weekday in ["WEDNESDAY", "THURSDAY"]:
            logging.info(f"📅 Normal Trading Day ({today_str} - {weekday}). Defaulting to SENSEX.")
            schedule.every().day.at("09:15").do(continuous_trading_session, index_symbol="SENSEX", expiry_date=sensex_expiry, cutoff_hour=15, cutoff_minute=15).tag('trading_jobs')
        else:
            logging.info(f"📅 Normal Trading Day ({today_str} - {weekday}). Defaulting to NIFTY.")
            schedule.every().day.at("09:15").do(continuous_trading_session, index_symbol="NIFTY", expiry_date=nifty_expiry, cutoff_hour=15, cutoff_minute=15).tag('trading_jobs')

if __name__ == "__main__":
    if logger.hasHandlers():
        logger.handlers.clear()
        logger.addHandler(file_handler)
        logger.addHandler(stream_handler)
        
    logging.info("System Architect Bot V3 Initialized...")

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

        if today_str == nifty_expiry:
            morning_idx, afternoon_idx = "NIFTY", "SENSEX"
            morning_exp, afternoon_exp = nifty_expiry, sensex_expiry
        elif today_str == sensex_expiry:
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
