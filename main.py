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

from data_feed import get_spot_price, get_option_chain, monitor_live_prices, get_fresh_option_quotes, get_spot_with_ohlc
from strategy import calculate_iron_butterfly_legs, risk_management_evaluator
from execution import place_iron_butterfly_basket, square_off_all

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HEARTBEAT_FILE = os.path.join(BASE_DIR, "engine_heartbeat.json")
GRACEFUL_STOP_FILE = os.path.join(BASE_DIR, "graceful_stop_flag.txt")
MANUAL_EXIT_FILE = os.path.join(BASE_DIR, "manual_exit_flag.txt")
MANUAL_ENTRY_FILE = os.path.join(BASE_DIR, "manual_entry_flag.txt")
PID_FILE = os.path.join(BASE_DIR, "engine_pid.txt")
_last_heartbeat_write = 0.0

# ============================================================================
# THE DOUBLE-LOG FIX: Only add StreamHandler when running in a real terminal.
# When launched via dashboard's subprocess.Popen(stdout=log_file), stdout IS
# bot.log, so StreamHandler + RotatingFileHandler both write to bot.log = dupes.
# ============================================================================
from logging.handlers import TimedRotatingFileHandler
logger = logging.getLogger()
logger.setLevel(logging.INFO)
logger.propagate = False

if logger.hasHandlers():
    logger.handlers.clear()

log_file_path = os.path.join(BASE_DIR, "bot.log")
file_handler = TimedRotatingFileHandler(log_file_path, when="midnight", interval=1, backupCount=7, encoding="utf-8")
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
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', expiry_date):
        return False
    try:
        datetime.strptime(expiry_date, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def is_stale_expiry(expiry_date):
    try:
        return datetime.strptime(expiry_date, "%Y-%m-%d").date() < datetime.now().date()
    except ValueError:
        return True


def clear_startup_flags():
    for flag_path in (GRACEFUL_STOP_FILE, MANUAL_EXIT_FILE, MANUAL_ENTRY_FILE):
        if os.path.exists(flag_path):
            try:
                os.remove(flag_path)
                logging.info("Cleared stale startup flag: %s", os.path.basename(flag_path))
            except Exception as err:
                logging.warning("Could not clear stale startup flag %s: %s", flag_path, err)


def mark_engine_stopped():
    write_heartbeat("STOPPED")
    if os.path.exists(PID_FILE):
        try:
            os.remove(PID_FILE)
        except Exception:
            pass


def write_heartbeat(status="RUNNING"):
    global _last_heartbeat_write
    now_ts = time.time()
    if now_ts - _last_heartbeat_write < config.HEARTBEAT_INTERVAL_SECONDS and status == "RUNNING":
        return

    payload = {
        "ts": now_ts,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": status,
        "pid": os.getpid(),
    }
    temp_path = HEARTBEAT_FILE + ".tmp"
    try:
        with open(temp_path, "w") as f:
            json.dump(payload, f)
        
        # Retry loop for Windows file lock (WinError 5)
        for _ in range(5):
            try:
                os.replace(temp_path, HEARTBEAT_FILE)
                _last_heartbeat_write = now_ts
                break
            except PermissionError:
                time.sleep(0.05)
    except Exception as err:
        logging.warning(f"Heartbeat write failed: {err}")


def consume_graceful_stop():
    if os.path.exists(GRACEFUL_STOP_FILE):
        try:
            os.remove(GRACEFUL_STOP_FILE)
        except Exception:
            pass
        return True
    return False


def calendar_blocks_trading():
    errors = config.validate_expiry_calendar()
    if errors:
        for error in errors:
            logging.critical("Expiry calendar validation failed: %s", error)
        import notifier
        notifier.send_telegram_alert(
            "<b>EXPIRY CALENDAR INVALID</b>\n" + "\n".join(errors)
        )
        return True
    return False


def current_expiries():
    return config.get_next_expiry("NIFTY"), config.get_next_expiry("SENSEX")


def session_for_time(now_dt, nifty_expiry, sensex_expiry):
    today_str = now_dt.strftime("%Y-%m-%d")
    current_time = now_dt.time()
    afternoon_start = datetime.strptime("12:31", "%H:%M").time()

    if today_str == nifty_expiry:
        if current_time >= afternoon_start:
            return "SENSEX", sensex_expiry, 15, 25
        return "NIFTY", nifty_expiry, 12, 30

    if today_str == sensex_expiry:
        if current_time >= afternoon_start:
            return "NIFTY", nifty_expiry, 15, 25
        return "SENSEX", sensex_expiry, 12, 30

    default_idx = "SENSEX" if now_dt.strftime("%A").upper() in ["WEDNESDAY", "THURSDAY"] else "NIFTY"
    default_exp = sensex_expiry if default_idx == "SENSEX" else nifty_expiry
    return default_idx, default_exp, 15, 15


def exit_prices_from_rest(legs):
    quotes = get_fresh_option_quotes(list(legs.values()))
    if not quotes:
        return None
    prices = {
        "sell_ce": quotes.get(legs["sell_ce"], 0),
        "sell_pe": quotes.get(legs["sell_pe"], 0),
        "buy_ce": quotes.get(legs["buy_ce"], 0),
        "buy_pe": quotes.get(legs["buy_pe"], 0),
    }
    return prices if all(value > 0 for value in prices.values()) else None


def initialize_sniper_state(entry_prices):
    entry_net = (entry_prices["sell_ce"] + entry_prices["sell_pe"]) - (entry_prices["buy_ce"] + entry_prices["buy_pe"])
    state_manager.update_many({
        "sniper_state": "INITIAL",
        "entry_net_premium": round(entry_net, 4),
        "sniper_targets_enabled": config.SNIPER_TARGETS_ENABLED,
        "sniper_target_pct": config.SNIPER_TARGET_PCT,
        "atm_drift_ratio": 0.0,
        "live_net_premium": round(entry_net, 4),
        "current_profit_pct": 0.0,
        "catastrophe_threshold": round(entry_net * config.SNIPER_CATASTROPHE_MULTIPLIER, 4),
    })


def deploy_single_sniper_trade(index_symbol, expiry_date, reason="REGULAR"):
    logging.info("Deploying %s Sniper & Shield Iron Butterfly for %s...", reason, index_symbol)
    spot = get_spot_price(index_symbol)
    chain = get_option_chain(index_symbol, expiry_date) if spot else None
    if not spot or not chain:
        logging.critical("Cannot deploy %s trade: spot found=%s, chain found=%s.", reason, bool(spot), bool(chain))
        return False

    legs, entry_prices, strikes = calculate_iron_butterfly_legs(
        index_symbol, spot, chain, buy_leg_percent=config.BUY_LEG_PERCENT
    )
    if not legs:
        logging.critical("Cannot deploy %s trade: wide-wing calculation failed.", reason)
        return False

    fresh_quotes = get_fresh_option_quotes(list(legs.values()))
    if fresh_quotes:
        for leg_name, token in legs.items():
            if token in fresh_quotes and fresh_quotes[token] > 0:
                entry_prices[leg_name] = fresh_quotes[token]
        logging.info("Absolute real-time entry prices: %s", entry_prices)
    else:
        logging.warning("Fresh quote sync failed. Falling back to option chain prices.")

    execution_success = place_iron_butterfly_basket(
        legs, index_symbol, entry_prices, strikes, spot_price=spot
    )
    if execution_success:
        initialize_sniper_state(entry_prices)
        state_manager.update_state("recenter_reason", reason if reason != "REGULAR" else "")
    return execution_success


def monitor_with_reconnects(legs, index_symbol):
    reconnects = 0
    while True:
        write_heartbeat("MONITORING")
        stop_loss_hit, exit_prices = monitor_live_prices(legs, risk_management_evaluator)
        if stop_loss_hit != "SOCKET_DEAD":
            return stop_loss_hit, exit_prices

        reconnects += 1
        state_manager.update_state("socket_reconnects", reconnects)
        logging.critical(
            f"WebSocket died for {index_symbol}; reconnect attempt {reconnects}/{config.MAX_SOCKET_RECONNECTS}."
        )

        active_state = state_manager.load_state()
        if reconnects <= config.MAX_SOCKET_RECONNECTS and active_state and active_state.get("active"):
            time.sleep(10)
            continue

        fresh_exit_prices = exit_prices_from_rest(legs)
        if fresh_exit_prices:
            logging.critical("WebSocket failed repeatedly. Fresh REST quotes available; forcing square off.")
            return "SOCKET_DEAD_EXIT", fresh_exit_prices

        import notifier
        notifier.send_telegram_alert(
            f"<b>SOCKET DEAD: MANUAL ACTION REQUIRED</b>\n"
            f"{index_symbol}: monitor could not reconnect and REST quotes were unavailable."
        )
        return "SOCKET_DEAD_FATAL", {}


def _minutes_to_cutoff(cutoff_hour, cutoff_minute, now=None):
    now = now or datetime.now()
    cutoff = now.replace(hour=cutoff_hour, minute=cutoff_minute, second=0, microsecond=0)
    return (cutoff - now).total_seconds() / 60.0


def _combined_premium(prices):
    if not prices:
        return 0.0
    return sum(float(prices.get(key, 0.0) or 0.0) for key in ("sell_ce", "sell_pe", "buy_ce", "buy_pe"))


def _mapped_leg_prices(legs, quotes):
    prices = {
        leg_name: float(quotes.get(token, 0.0) or 0.0)
        for leg_name, token in legs.items()
    }
    return prices if all(value > 0 for value in prices.values()) else None


def post_emergency_reentry_allowed(
    index_symbol,
    legs,
    exit_prices,
    exit_reason,
    cutoff_hour,
    cutoff_minute,
    reference_spot=0.0,
):
    if not config.POST_EMERGENCY_REENTRY_ENABLED:
        return True

    minutes_left = _minutes_to_cutoff(cutoff_hour, cutoff_minute)
    if minutes_left < config.POST_EMERGENCY_REENTRY_MIN_MINUTES_TO_CUTOFF:
        logging.critical(
            "%s re-entry blocked: %.1f minutes left before %02d:%02d cutoff.",
            exit_reason,
            minutes_left,
            cutoff_hour,
            cutoff_minute,
        )
        return False

    cooldown = float(config.POST_EMERGENCY_REENTRY_COOLDOWN_SECONDS)
    if cooldown > 0:
        logging.info("Cooling down %.0f seconds before post-%s re-entry check.", cooldown, exit_reason)
        time.sleep(cooldown)

    minutes_left = _minutes_to_cutoff(cutoff_hour, cutoff_minute)
    if minutes_left < config.POST_EMERGENCY_REENTRY_MIN_MINUTES_TO_CUTOFF:
        logging.critical(
            "%s re-entry blocked after cooldown: %.1f minutes left before cutoff.",
            exit_reason,
            minutes_left,
        )
        return False

    quotes = get_fresh_option_quotes(list(legs.values()))
    current_prices = _mapped_leg_prices(legs, quotes)
    if not current_prices:
        logging.critical("%s re-entry blocked: fresh option quotes unavailable.", exit_reason)
        return False

    exit_combined = _combined_premium(exit_prices)
    current_combined = _combined_premium(current_prices)
    premium_change = abs(current_combined - exit_combined) / exit_combined if exit_combined > 0 else 1.0

    state = state_manager.load_state() or {}
    reference_spot = float(reference_spot or state.get("last_spot") or state.get("entry_spot") or 0.0)
    current_spot = get_spot_price(index_symbol) or 0.0
    spot_change = abs(float(current_spot) - reference_spot) / reference_spot if reference_spot > 0 and current_spot else 0.0

    if premium_change > config.POST_EMERGENCY_MAX_PREMIUM_CHANGE_PCT:
        logging.critical(
            "%s re-entry blocked: combined premium still unstable (%.2f%% > %.2f%%).",
            exit_reason,
            premium_change * 100,
            config.POST_EMERGENCY_MAX_PREMIUM_CHANGE_PCT * 100,
        )
        return False

    if spot_change > config.POST_EMERGENCY_MAX_SPOT_CHANGE_PCT:
        logging.critical(
            "%s re-entry blocked: spot still moving fast (%.2f%% > %.2f%%).",
            exit_reason,
            spot_change * 100,
            config.POST_EMERGENCY_MAX_SPOT_CHANGE_PCT * 100,
        )
        return False

    logging.info(
        "%s re-entry allowed: premium change %.2f%%, spot change %.2f%%.",
        exit_reason,
        premium_change * 100,
        spot_change * 100,
    )
    return True


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
    write_heartbeat(f"SESSION:{index_symbol}")
    halt_without_final_squareoff = False

    # ================================================================
    # EXPIRY DATE VALIDATION GUARD
    # ================================================================
    if not is_valid_expiry(expiry_date) or is_stale_expiry(expiry_date):
        logging.critical(
            f"❌ INVALID EXPIRY DATE: '{expiry_date}' for {index_symbol}. "
            f"Cannot deploy trades. Please fix expiries.json before starting the engine."
        )
        import notifier
        notifier.send_telegram_alert(
            f"❌ <b>INVALID EXPIRY DATE!</b>\n"
            f"{index_symbol}: '{expiry_date}'\n"
            f"Fix expiries.json and restart."
        )
        return

    if os.path.exists(MANUAL_EXIT_FILE):
        os.remove(MANUAL_EXIT_FILE)
        
    # ================================================================
    # PHASE 0: BTST CARRY FORWARD RECOVERY
    # ================================================================
    btst_exit_reason = None  # Track why the carry-forward exited
    
    state = state_manager.load_state()
    if state and state.get("active"):
        logging.critical(f"🌙 BTST CARRY FORWARD DETECTED: Waking up existing {index_symbol} trade.")
        stop_loss_hit, exit_prices = monitor_with_reconnects(state['legs'], index_symbol)
        
        if stop_loss_hit:
            btst_exit_reason = stop_loss_hit  # Remember why we exited
            
            if stop_loss_hit == "SOCKET_DEAD_FATAL":
                logging.critical("Socket recovery failed on carry-forward. Halting with state retained.")
                return
            elif stop_loss_hit == "SOCKET_DEAD_EXIT":
                logging.critical("Socket recovery failed on carry-forward. Squaring off with REST quotes...")
            elif stop_loss_hit == "SNIPER_TARGET":
                logging.critical(f"💰 Sniper profit exit on {index_symbol} Carry Forward trade! Squaring off...")
            elif stop_loss_hit == "CATASTROPHE_KILL":
                logging.warning(f"🚨 Catastrophe kill on {index_symbol} Carry Forward trade! Squaring off...")
            elif stop_loss_hit == "ATM_DRIFT":
                logging.critical(f"🌊 ATM Drift detected on {index_symbol} Carry Forward. Structure broken — squaring off...")
            elif stop_loss_hit == "TIME_EXIT":
                logging.critical(f"⏰ EOD Cutoff triggered on Carry Forward trade. Squaring off...")
            elif stop_loss_hit == "MANUAL_EXIT":
                logging.critical(f"🛑 Manual Exit on {index_symbol} Carry Forward. Squaring off...")
            else:
                logging.info(f"🔄 Exit Signal ({stop_loss_hit}) received on Carry Forward. Squaring off...")
                
            square_off_all(exit_prices, exit_reason=stop_loss_hit)
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

        if btst_exit_reason in ("CATASTROPHE_KILL", "ATM_DRIFT"):
            if not post_emergency_reentry_allowed(
                index_symbol,
                state["legs"],
                exit_prices,
                btst_exit_reason,
                cutoff_hour,
                cutoff_minute,
                reference_spot=state.get("last_spot") or state.get("entry_spot") or 0.0,
            ):
                logging.critical("Post-emergency guard blocked fresh carry-forward re-entry. Session halted.")
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
            write_heartbeat("GAP_WAIT")
            now = datetime.now()
            if now.hour > cutoff_hour or (now.hour == cutoff_hour and now.minute >= cutoff_minute):
                logging.info(f"⏰ Cutoff reached during gap wait. Ending session.")
                return
            
            if os.path.exists(MANUAL_EXIT_FILE):
                os.remove(MANUAL_EXIT_FILE)
                logging.critical("🛑 MANUAL EXIT triggered during Gap Filter wait. Halting session.")
                return
                
            if os.path.exists(MANUAL_ENTRY_FILE):
                os.remove(MANUAL_ENTRY_FILE)
                logging.critical("▶️ MANUAL ENTRY triggered! Skipping gap wait.")
                break
                
            if consume_graceful_stop():
                logging.critical("Graceful stop requested during Gap Filter wait. Halting session.")
                mark_engine_stopped()
                raise SystemExit(0)
                
            time.sleep(5)
        logging.info("✅ Gap settle period complete. Resuming normal operations.")

    # ================================================================
    # PHASE 2: SAFE ENTRY WINDOW WAIT
    # ================================================================
    if cutoff_hour == 12: 
        target_entry_time = datetime.strptime("09:20", "%H:%M").time()
        logged_wait = False
        while datetime.now().time() < target_entry_time:
            write_heartbeat("ENTRY_WAIT")
            if consume_graceful_stop():
                logging.critical("Graceful stop requested during entry wait. Halting session.")
                mark_engine_stopped()
                raise SystemExit(0)
                
            if os.path.exists(MANUAL_ENTRY_FILE):
                os.remove(MANUAL_ENTRY_FILE)
                logging.critical("▶️ MANUAL ENTRY triggered! Skipping safe entry window wait.")
                break
                
            if not logged_wait:
                logging.info("⏳ Waiting for 09:20 AM safe entry window before deploying fresh trade...")
                logged_wait = True
            time.sleep(2)

    # ================================================================
    # PHASE 3: MAIN TRADING LOOP (Sniper & Shield + API Retry Limit)
    # ================================================================
    api_retry_count = 0
    MAX_API_RETRIES = 5

    while True:
        write_heartbeat("RUNNING")
        now = datetime.now()
        
        state = state_manager.load_state()
        is_active = state and state.get("active", False)
        if not is_active and (now.hour > cutoff_hour or (now.hour == cutoff_hour and now.minute >= cutoff_minute)):
            logging.info(f"⏰ Soft Cutoff ({cutoff_hour}:{cutoff_minute:02d}) reached. No NEW {index_symbol} trades will be taken.")
            break 

        if consume_graceful_stop():
            logging.critical("Graceful stop requested. No new trades will be deployed.")
            mark_engine_stopped()
            raise SystemExit(0)

        state = state_manager.load_state()
        is_active = state and state.get("active", False)

        if is_active:
            logging.info(f"Resuming live risk monitoring for active {index_symbol} trade...")
            legs = state['legs']
            entry_prices = state['entry_prices']
        else:
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

            legs, entry_prices, strikes = calculate_iron_butterfly_legs(
                index_symbol, spot, chain, buy_leg_percent=config.BUY_LEG_PERCENT
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
            
            execution_success = place_iron_butterfly_basket(legs, index_symbol, entry_prices, strikes, spot_price=spot)
        
            if not execution_success:
                logging.critical("🛑 CRITICAL: Basket execution failed mid-flight! Halting session to prevent orphan legs.")
                import notifier
                notifier.send_telegram_alert(f"🚨 <b>URGENT ACTION REQUIRED!</b> 🚨\n{index_symbol} basket order failed mid-execution. Check Upstox App immediately.")
                break 

            initialize_sniper_state(entry_prices)
            
            state_manager.update_state("cutoff_hour", cutoff_hour)
            state_manager.update_state("cutoff_minute", cutoff_minute)
            

        logging.info("Entering live risk monitoring phase...")

        stop_loss_hit, exit_prices = monitor_with_reconnects(legs, index_symbol)

        # ================================================================
        # EXIT SIGNAL HANDLING
        # ================================================================
        
        if stop_loss_hit in ("MANUAL_EXIT", "GRACEFUL_STOP"):
            logging.critical(f"🛑 User stop/exit executed. Squaring off {index_symbol}...")
            square_off_all(exit_prices, exit_reason=stop_loss_hit)
            logging.critical("Trading paused for this session due to user stop/exit.")
            if stop_loss_hit == "GRACEFUL_STOP":
                mark_engine_stopped()
                raise SystemExit(0)
            break 

        elif stop_loss_hit == "SNIPER_TARGET":
            logging.critical("Sniper profit exit for %s: %s.", index_symbol, stop_loss_hit)
            square_off_all(exit_prices, exit_reason=stop_loss_hit)
            logging.info("Cooling down for 60 seconds before looking for new setups...")
            time.sleep(60)

        elif stop_loss_hit == "CATASTROPHE_KILL":
            logging.critical("Catastrophe kill for %s. Squaring off before re-entry safety check.", index_symbol)
            pre_exit_state = state_manager.load_state() or {}
            square_off_all(exit_prices, exit_reason=stop_loss_hit)
            if post_emergency_reentry_allowed(
                index_symbol,
                legs,
                exit_prices,
                stop_loss_hit,
                cutoff_hour,
                cutoff_minute,
                reference_spot=pre_exit_state.get("last_spot") or pre_exit_state.get("entry_spot") or 0.0,
            ):
                continue
            break

        elif stop_loss_hit == "ATM_DRIFT":
            pre_exit_state = state_manager.load_state() or {}
            logging.critical(f"🌊 ATM Drift exit for {index_symbol}! Structure compromised — squaring off...")
            square_off_all(exit_prices, exit_reason=stop_loss_hit)
            if post_emergency_reentry_allowed(
                index_symbol,
                legs,
                exit_prices,
                stop_loss_hit,
                cutoff_hour,
                cutoff_minute,
                reference_spot=pre_exit_state.get("last_spot") or pre_exit_state.get("entry_spot") or 0.0,
            ):
                continue
            break
        
        elif stop_loss_hit == "MARKET_CLOSED":
            logging.info(f"🛑 Live market feed ended for {index_symbol}. Proceeding to EOD evaluation.")
            break

        elif stop_loss_hit == "SOCKET_DEAD_EXIT":
            logging.critical(f"WebSocket recovery failed for {index_symbol}. Squaring off with REST quote snapshot.")
            square_off_all(exit_prices, exit_reason=stop_loss_hit)
            break

        elif stop_loss_hit == "SOCKET_DEAD_FATAL":
            logging.critical(f"WebSocket recovery failed for {index_symbol}. State retained for manual recovery.")
            halt_without_final_squareoff = True
            break

        elif stop_loss_hit == "TIME_EXIT":
            logging.critical(f"⏰ EOD Cutoff triggered for {index_symbol}. Squaring off and ending session.")
            square_off_all(exit_prices, exit_reason=stop_loss_hit)
            break 

        elif stop_loss_hit == "BTST_RECENTER":
            logging.critical("BTST unhealthy at cutoff. Squaring off and deploying one fresh centered carry trade.")
            square_off_all(exit_prices, exit_reason=stop_loss_hit)
            if deploy_single_sniper_trade(index_symbol, expiry_date, reason="BTST_RECENTER"):
                logging.critical("BTST recenter trade deployed successfully. Resuming monitoring.")
                continue
            else:
                logging.critical("BTST recenter entry failed. Going flat and ending session.")
                import notifier
                notifier.send_telegram_alert(
                    f"<b>BTST RECENTER FAILED</b>\n{index_symbol}: sick trade was closed, but fresh carry entry failed."
                )
            break

        elif stop_loss_hit:
            logging.warning(f"Stop Loss hit for {index_symbol}! Squaring off...")
            square_off_all(exit_prices, exit_reason=stop_loss_hit)
            logging.info("Cooling down for 60 seconds...")
            time.sleep(60)
        else:
            logging.error("WebSocket connection terminated unexpectedly.")
            break
        
    logging.info(f"--- END OF SESSION FOR {index_symbol} ---")
    write_heartbeat("IDLE")

    if halt_without_final_squareoff:
        logging.critical("Skipping final automatic square off because quote recovery failed. Manual broker check required.")
        return
    
    btst_file = os.path.join(BASE_DIR, "btst_flag.txt")
    if os.path.exists(btst_file) and open(btst_file, "r").read().strip() == "TRUE":
        state = state_manager.load_state()
        if state and state.get("active"):
            logging.critical(f"🌙 BTST ENABLED: Leaving position open for overnight carry-forward.")
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
                    
        square_off_all(exit_prices, exit_reason="FINAL_SAFETY")


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

    if calendar_blocks_trading():
        return

    nifty_expiry, sensex_expiry = current_expiries()

    # ================================================================
    # EXPIRY DAY STRATEGY:
    # The EXPIRING index trades in the MORNING (first half) to capture
    # rapid theta decay. The other index gets the afternoon slot.
    # ================================================================

    if today_str == nifty_expiry:
        logging.critical(f"🎯 NIFTY EXPIRY DETECTED ({today_str}). Expiry Strategy: Nifty MORNING (theta capture), Sensex AFTERNOON.")
        # Nifty in the morning (expiring — capture rapid theta decay)
        schedule.every().day.at(config.EXPIRY_ENTRY_TIME).do(
            continuous_trading_session, index_symbol="NIFTY", 
            expiry_date=nifty_expiry, cutoff_hour=12, cutoff_minute=30
        ).tag('trading_jobs')
        # Sensex in the afternoon (safe, not expiring)
        schedule.every().day.at("12:31").do(
            continuous_trading_session, index_symbol="SENSEX", 
            expiry_date=sensex_expiry, cutoff_hour=15, cutoff_minute=25
        ).tag('trading_jobs')

    elif today_str == sensex_expiry:
        logging.critical(f"🎯 SENSEX EXPIRY DETECTED ({today_str}). Expiry Strategy: Sensex MORNING (theta capture), Nifty AFTERNOON.")
        # Sensex in the morning (expiring — capture rapid theta decay)
        schedule.every().day.at(config.EXPIRY_ENTRY_TIME).do(
            continuous_trading_session, index_symbol="SENSEX", 
            expiry_date=sensex_expiry, cutoff_hour=12, cutoff_minute=30
        ).tag('trading_jobs')
        # Nifty in the afternoon (safe, not expiring)
        schedule.every().day.at("12:31").do(
            continuous_trading_session, index_symbol="NIFTY", 
            expiry_date=nifty_expiry, cutoff_hour=15, cutoff_minute=25
        ).tag('trading_jobs')
    
    else:
        weekday = now.strftime("%A").upper()
        if weekday in ["WEDNESDAY", "THURSDAY"]:
            logging.info(f"📅 Normal Trading Day ({today_str} - {weekday}). Defaulting to SENSEX.")
            schedule.every().day.at(config.NORMAL_ENTRY_TIME).do(continuous_trading_session, index_symbol="SENSEX", expiry_date=sensex_expiry, cutoff_hour=15, cutoff_minute=15).tag('trading_jobs')
        else:
            logging.info(f"📅 Normal Trading Day ({today_str} - {weekday}). Defaulting to NIFTY.")
            schedule.every().day.at(config.NORMAL_ENTRY_TIME).do(continuous_trading_session, index_symbol="NIFTY", expiry_date=nifty_expiry, cutoff_hour=15, cutoff_minute=15).tag('trading_jobs')


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    clear_startup_flags()
    # Handler setup already done at module level — no need to duplicate here
        
    logging.info("Sniper & Shield Bot Initialized — State Architecture Active 🏛️")
    logging.info(f"  Buy Leg Premium Target: {config.BUY_LEG_PERCENT}%")
    logging.info(
        f"  Sniper Target: {'ON' if config.SNIPER_TARGETS_ENABLED else 'OFF'} "
        f"({config.SNIPER_TARGET_PCT:.1f}%)"
    )
    logging.info(f"  ATM Drift Ejector: {config.ATM_DRIFT_EJECT_THRESHOLD:.2f}x")
    logging.info(f"  Catastrophe Kill: {config.SNIPER_CATASTROPHE_MULTIPLIER:.2f}x entry net premium")
    logging.info(f"  Opening Range Gap Filter: ON ({config.GAP_THRESHOLD_PCT*100:.1f}% threshold, {config.GAP_SETTLE_MINUTES}min settle)")
    logging.info(f"  Expiry Day Strategy: Expiring index trades MORNING, other AFTERNOON")
    logging.info(f"  Weekend Guard: ON")

    calendar_invalid = calendar_blocks_trading()
    nifty_expiry, sensex_expiry = current_expiries()

    if not calendar_invalid:
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
            cutoff_minute=25
        )
    
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    eod_cutoff = datetime.strptime("15:25", "%H:%M").time()
    
    if not calendar_invalid and not (hasattr(config, 'MARKET_HOLIDAYS') and today_str in config.MARKET_HOLIDAYS) and now.weekday() < 5:
        current_time = now.time()
        morning_start = datetime.strptime(config.NORMAL_ENTRY_TIME, "%H:%M").time()
        afternoon_start = datetime.strptime("12:31", "%H:%M").time()

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
            continuous_trading_session(afternoon_idx, afternoon_exp, 15, 25)

    logging.info("Waiting for scheduled events...")
    while True:
        write_heartbeat("WAITING")
        schedule.run_pending()

        if consume_graceful_stop():
            logging.critical("Graceful stop requested while idle. Engine stopping.")
            mark_engine_stopped()
            raise SystemExit(0)
        
        if os.path.exists(MANUAL_ENTRY_FILE):
            try:
                os.remove(MANUAL_ENTRY_FILE)
            except Exception:
                pass
            
            logging.critical("▶️ MANUAL ENTRY triggered from idle state!")
            now_dt = datetime.now()
            today_date_str = now_dt.strftime("%Y-%m-%d")
            
            idx, exp, manual_cutoff_hour, manual_cutoff_minute = session_for_time(
                now_dt,
                nifty_expiry,
                sensex_expiry,
            )
            
            if calendar_invalid:
                logging.critical("Manual entry ignored because expiry calendar is invalid.")
            elif datetime.now().time() >= eod_cutoff:
                logging.info("Soft Cutoff (15:15) reached. No manual entry will be taken.")
            else:
                logging.info(
                    "Manual entry resolved to %s session with cutoff %02d:%02d.",
                    idx,
                    manual_cutoff_hour,
                    manual_cutoff_minute,
                )
                continuous_trading_session(idx, exp, manual_cutoff_hour, manual_cutoff_minute)
                logging.info("Returned to idle state after manual entry session.")
            
        time.sleep(1)
