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

from data_feed import (
    get_spot_price,
    get_option_chain,
    monitor_live_prices,
    get_fresh_option_quotes,
    get_spot_with_ohlc,
    get_india_vix,
    get_intraday_candles,
)
from strategy import calculate_iron_butterfly_legs, risk_management_evaluator
from btst_vix_router import (
    candles_market_profile,
    build_telemetry_flow_contexts,
    is_btst_momentum_time,
    route_adaptive_btst_strategy,
    route_command_center_strategy,
    route_btst_momentum_strategy,
    route_intraday_neutral_strategy,
)
from market_context import days_to_expiry
from execution import place_iron_butterfly_basket, place_option_spread_basket, square_off_all, slice_neutral_side
from position_sizing import calculate_position_size

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
# console.log, so StreamHandler + RotatingFileHandler both write to bot.log = dupes.
# ============================================================================
from logging.handlers import TimedRotatingFileHandler
import threading

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

# Global excepthooks to capture uncaught exceptions in bot.log
def handle_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logger.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))

sys.excepthook = handle_exception

def handle_thread_exception(args):
    logger.error(
        "Uncaught thread exception in thread %s", args.thread.name,
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback)
    )

threading.excepthook = handle_thread_exception


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


def _parse_hhmm(value, default="09:45"):
    try:
        return datetime.strptime(str(value), "%H:%M").time()
    except ValueError:
        return datetime.strptime(default, "%H:%M").time()


def fresh_entry_gate_open(now=None):
    now = now or datetime.now()
    return now.time() >= _parse_hhmm(config.ENTRY_STANDBY_TIME)


def fresh_entry_cutoff_reached(now=None):
    now = now or datetime.now()
    return now.time() >= _parse_hhmm(config.FRESH_ENTRY_CUTOFF_TIME, default="15:10")


def wait_for_fresh_entry_gate(cutoff_hour, cutoff_minute):
    logged_wait = False
    while not fresh_entry_gate_open():
        write_heartbeat("STANDBY")
        now = datetime.now()
        if now.hour > cutoff_hour or (now.hour == cutoff_hour and now.minute >= cutoff_minute):
            logging.info("Cutoff reached during 09:45 standby gate. Ending session.")
            return False
        if consume_graceful_stop():
            logging.critical("Graceful stop requested during 09:45 standby gate. Halting session.")
            mark_engine_stopped()
            raise SystemExit(0)
        if not logged_wait:
            logging.info("STANDBY: fresh entries blocked until %s.", config.ENTRY_STANDBY_TIME)
            logged_wait = True
        time.sleep(2)
    return True


def sleep_until_next_flow_poll(cutoff_hour, cutoff_minute):
    deadline = time.time() + float(config.FLOW_POLL_SECONDS)
    while time.time() < deadline:
        write_heartbeat("NO_TRADE_STANDBY")
        now = datetime.now()
        if fresh_entry_cutoff_reached(now):
            logging.info("Fresh entry cutoff %s reached while waiting for next OI-flow poll.", config.FRESH_ENTRY_CUTOFF_TIME)
            return False
        if now.hour > cutoff_hour or (now.hour == cutoff_hour and now.minute >= cutoff_minute):
            logging.info("Cutoff reached while waiting for next OI-flow poll.")
            return False
        if consume_graceful_stop():
            logging.critical("Graceful stop requested during OI-flow standby. Halting session.")
            mark_engine_stopped()
            raise SystemExit(0)
        time.sleep(min(5, max(0.1, deadline - time.time())))
    return True


def safe_trading_expiry(index_symbol, now=None):
    now = now or datetime.now()
    today = now.date()
    calendar = config.load_expiry_calendar()
    holidays = set(calendar.get("HOLIDAYS", []))
    for expiry in calendar.get(index_symbol, []):
        try:
            expiry_date = datetime.strptime(str(expiry), "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        if str(expiry) in holidays or expiry_date.weekday() >= 5:
            continue
        if expiry_date > today:
            return str(expiry)
    fallback = config.get_next_expiry(index_symbol, now)
    try:
        if datetime.strptime(str(fallback), "%Y-%m-%d").date() <= today:
            return "UNKNOWN"
    except (TypeError, ValueError):
        return fallback
    return fallback


def current_expiries(now=None):
    now = now or datetime.now()
    return safe_trading_expiry("NIFTY", now), safe_trading_expiry("SENSEX", now)


def session_for_time(now_dt, nifty_expiry, sensex_expiry):
    default_idx = "SENSEX" if now_dt.strftime("%A").upper() in ["WEDNESDAY", "THURSDAY"] else "NIFTY"
    default_exp = safe_trading_expiry(default_idx, now_dt)
    return default_idx, default_exp, 15, 25


def exit_prices_from_rest(legs):
    quotes = get_fresh_option_quotes(list(legs.values()))
    if not quotes:
        return None
    prices = {
        leg_name: quotes.get(token, 0)
        for leg_name, token in legs.items()
    }
    return prices if all(value > 0 for value in prices.values()) else None


def lot_multiple_for_index(index_symbol):
    return config.NIFTY_LOT_MULTIPLE if index_symbol == "NIFTY" else config.SENSEX_LOT_MULTIPLE


def net_from_order_sequence(prices, order_sequence):
    net = 0.0
    for leg_name, transaction_type in order_sequence or []:
        price = float(prices.get(leg_name, 0.0) or 0.0)
        if str(transaction_type).upper() == "SELL":
            net += price
        elif str(transaction_type).upper() == "BUY":
            net -= price
    return net


def spread_width(strikes):
    values = [float(value) for key, value in (strikes or {}).items() if key != "atm" and value not in (None, "")]
    return max(values) - min(values) if len(values) >= 2 else 0.0


def strategy_params_for_type(strategy_type):
    strategy_type = str(strategy_type or "").upper()
    if strategy_type == "IRON_CONDOR":
        return {
            "target_pct": config.IRON_CONDOR_TARGET_PCT,
            "catastrophe_multiplier": config.IRON_CONDOR_CATASTROPHE_MULTIPLIER,
            "atm_drift_points": config.IRON_CONDOR_ATM_DRIFT_POINTS,
            "vix_activation": config.IRON_CONDOR_VIX_ACTIVATION,
        }
    if strategy_type == "IRON_BUTTERFLY":
        return {
            "target_pct": config.IRON_BUTTERFLY_TARGET_PCT,
            "catastrophe_multiplier": config.IRON_BUTTERFLY_CATASTROPHE_MULTIPLIER,
            "atm_drift_points": config.IRON_BUTTERFLY_ATM_DRIFT_POINTS,
            "vix_activation": config.IRON_BUTTERFLY_VIX_ACTIVATION,
        }
    return {
        "target_pct": config.DIRECTIONAL_TARGET_PCT,
        "catastrophe_multiplier": config.DIRECTIONAL_CATASTROPHE_MULTIPLIER,
        "btst_auto_exit_roc_pct": config.DIRECTIONAL_BTST_AUTO_EXIT_ROC_PCT,
    }


def max_profit_for_route(strategy_type, entry_prices, strikes, quantity, order_sequence=None):
    if order_sequence:
        entry_net = net_from_order_sequence(entry_prices, order_sequence)
        if entry_net >= 0:
            return max(0.0, entry_net * int(quantity))
        return max(0.0, (spread_width(strikes) + entry_net) * int(quantity))
    entry_net = (entry_prices["sell_ce"] + entry_prices["sell_pe"]) - (entry_prices["buy_ce"] + entry_prices["buy_pe"])
    return max(0.0, entry_net * int(quantity))


def initialize_sniper_state(
    entry_prices,
    strategy_type="IRON_BUTTERFLY",
    drift_threshold=None,
    metadata=None,
    strategy_params=None,
    sizing=None,
    expiry_date=None,
    order_sequence=None,
):
    active_state = state_manager.load_state() or {}
    entry_prices = active_state.get("entry_prices", entry_prices)
    quantity = int(active_state.get("quantity", 0) or 0)
    strikes = active_state.get("strikes", {})
    entry_net = (
        net_from_order_sequence(entry_prices, order_sequence)
        if order_sequence
        else (entry_prices["sell_ce"] + entry_prices["sell_pe"]) - (entry_prices["buy_ce"] + entry_prices["buy_pe"])
    )
    drift_threshold = config.ATM_DRIFT_EJECT_THRESHOLD if drift_threshold is None else drift_threshold
    strategy_params = strategy_params or strategy_params_for_type(strategy_type)
    metadata = metadata or {}
    market_context = metadata.get("market_context", {})
    sizing_data = sizing or active_state.get("execution_info", {}).get("sizing", {})
    broker_lot_size = int(sizing_data.get("lot_multiple") or lot_multiple_for_index(active_state.get("index_symbol", "NIFTY")))
    total_lots = int(sizing_data.get("lots_to_deploy") or (quantity // broker_lot_size if broker_lot_size else 0))
    target_pct = strategy_params.get("target_pct", config.SNIPER_TARGET_PCT)
    catastrophe_multiplier = strategy_params.get("catastrophe_multiplier", config.SNIPER_CATASTROPHE_MULTIPLIER)
    max_profit = max_profit_for_route(strategy_type, entry_prices, strikes, quantity, order_sequence=order_sequence)
    state_manager.update_many({
        "sniper_state": "INITIAL",
        "strategy_type": strategy_type,
        "entry_net_premium": round(entry_net, 4),
        "sniper_targets_enabled": config.SNIPER_TARGETS_ENABLED,
        "sniper_target_pct": target_pct,
        "atm_drift_eject_threshold": round(drift_threshold, 4),
        "atm_drift_ratio": 0.0,
        "live_net_premium": round(entry_net, 4),
        "current_profit_pct": 0.0,
        "catastrophe_threshold": round(entry_net * catastrophe_multiplier, 4),
        "route_metadata": metadata,
        "market_context": market_context,
        "strategy_params": strategy_params,
        "sizing": sizing_data,
        "capital_deployed": sizing_data.get("capital_deployed", active_state.get("capital_deployed", 0.0)),
        "max_profit_rupees": round(max_profit, 2),
        "expiry_date": expiry_date or active_state.get("expiry_date", ""),
        "effective_expiry_date": expiry_date or active_state.get("effective_expiry_date", ""),
        "dte": market_context.get("dte", days_to_expiry(expiry_date)) if expiry_date else active_state.get("dte", 0),
        "entry_regime_signal": metadata.get("entry_regime_signal") or market_context.get("flow_signal", ""),
        "latest_regime_signal": market_context.get("flow_signal", ""),
        "regime_reversal_count": 0,
        "last_regime_check_ts": time.time(),
        "straddle_premium": market_context.get("straddle_premium"),
        "previous_straddle_premium": market_context.get("previous_straddle_premium"),
        "oi_flow_snapshot": metadata.get("oi_flow_snapshot") or market_context.get("oi_flow_snapshot", {}),
        "broker_lot_size": broker_lot_size,
        "total_lots_deployed": total_lots,
        "total_quantity": int(quantity),
        "margin_blocked": sizing_data.get("capital_deployed", 0.0),
    })


def build_intraday_neutral_route(index_symbol, spot, chain):
    live_vix = get_india_vix()
    route = route_intraday_neutral_strategy(
        index_symbol,
        spot,
        chain,
        live_vix,
        calculate_iron_butterfly_legs,
    )
    if not route.legs:
        logging.critical("Neutral strategy route rejected: %s", route.no_trade_reason)
        return None
    logging.info(
        "Neutral VIX route selected %s for %s: VIX=%s, drift threshold=%.2f.",
        route.strategy_type,
        index_symbol,
        route.metadata.get("india_vix"),
        route.drift_threshold,
    )
    return route


def build_command_center_route(index_symbol, expiry_date, spot, chain, previous_snapshot=None):
    live_vix = get_india_vix()
    route = route_command_center_strategy(
        index_symbol,
        expiry_date,
        spot,
        chain,
        live_vix,
        calculate_iron_butterfly_legs,
        previous_snapshot=previous_snapshot,
    )
    if not route.legs:
        logging.info("Command-center route standby: %s", route.no_trade_reason)
        return route
    context = route.metadata.get("market_context", {})
    logging.info(
        "Command-center route selected %s for %s: flow=%s, straddle=%s, DTE=%s.",
        route.strategy_type,
        index_symbol,
        context.get("flow_signal"),
        context.get("straddle_signal"),
        context.get("dte"),
    )
    return route


def collect_opening_telemetry(index_symbol, expiry_date):
    logging.info("Starting opening telemetry collector for %s until %s.", index_symbol, config.ENTRY_STANDBY_TIME)
    previous_snapshot = None
    entry_time = _parse_hhmm(config.ENTRY_STANDBY_TIME)
    while datetime.now().time() < entry_time:
        write_heartbeat(f"TELEMETRY:{index_symbol}")
        if consume_graceful_stop():
            logging.critical("Graceful stop requested during telemetry collection. Halting session.")
            mark_engine_stopped()
            raise SystemExit(0)

        spot = get_spot_price(index_symbol)
        chain = get_option_chain(index_symbol, expiry_date) if spot else None
        india_vix = get_india_vix()
        if spot and chain:
            instant_context, cumulative_context = build_telemetry_flow_contexts(
                index_symbol,
                expiry_date,
                spot,
                chain,
                india_vix,
                previous_snapshot=previous_snapshot,
            )
            if instant_context.oi_flow_snapshot:
                previous_snapshot = instant_context.oi_flow_snapshot
            active_context = cumulative_context or instant_context
            logging.info(
                "Telemetry snapshot: flow=%s/%s straddle=%s/%s.",
                instant_context.flow_signal,
                active_context.flow_signal,
                instant_context.straddle_signal,
                active_context.straddle_signal,
            )
        else:
            logging.warning("Telemetry snapshot skipped: spot=%s chain=%s.", bool(spot), bool(chain))

        deadline = time.time() + float(config.FLOW_POLL_SECONDS)
        while datetime.now().time() < entry_time and time.time() < deadline:
            write_heartbeat(f"TELEMETRY:{index_symbol}")
            if consume_graceful_stop():
                logging.critical("Graceful stop requested during telemetry collection. Halting session.")
                mark_engine_stopped()
                raise SystemExit(0)
            time.sleep(min(5, max(0.1, deadline - time.time())))
    logging.info("Opening telemetry collector finished for %s.", index_symbol)


def sync_entry_prices_with_quotes(legs, entry_prices):
    logging.info("Synchronizing with Live Exchange Quotes to bypass cached API data...")
    fresh_quotes = get_fresh_option_quotes(list(legs.values()))
    if fresh_quotes:
        for leg_name, token in legs.items():
            if token in fresh_quotes and fresh_quotes[token] > 0:
                entry_prices[leg_name] = fresh_quotes[token]
        logging.info("Absolute Real-Time Entry Prices: %s", entry_prices)
    else:
        logging.warning("Sync failed. Falling back to option chain prices.")
    return entry_prices


def execute_command_center_route(index_symbol, expiry_date, spot, route, reason="REGULAR", carry_overnight=False):
    sizing = calculate_position_size(
        config.ENVIRONMENT,
        route.strategy_type,
        route.metadata.get("india_vix") or route.metadata.get("market_context", {}).get("vix"),
        config.VIRTUAL_CAPITAL,
        index_symbol,
        lot_multiple_for_index(index_symbol),
        route=route,
    )
    if sizing.get("status") != "APPROVED":
        logging.critical("Entry rejected before order placement by position sizing: %s", sizing)
        import notifier
        notifier.send_telegram_alert(
            f"<b>ENTRY REJECTED BY SIZING</b>\n"
            f"{index_symbol} {route.strategy_type}: {sizing.get('reason', 'Rejected')}"
        )
        return False

    entry_prices = sync_entry_prices_with_quotes(route.legs, route.entry_prices)
    strategy_params = strategy_params_for_type(route.strategy_type)
    if route.order_sequence:
        execution_success = place_option_spread_basket(
            route.legs,
            index_symbol,
            entry_prices,
            route.strikes,
            route.order_sequence,
            route.strategy_type,
            spot_price=spot,
            carry_overnight=carry_overnight,
            metadata={**route.metadata, "reason": reason},
            quantity=sizing["quantity"],
            sizing=sizing,
        )
    else:
        execution_success = place_iron_butterfly_basket(
            route.legs,
            index_symbol,
            entry_prices,
            route.strikes,
            spot_price=spot,
            quantity=sizing["quantity"],
            strategy_type=route.strategy_type,
            sizing=sizing,
        )

    if execution_success:
        initialize_sniper_state(
            entry_prices,
            strategy_type=route.strategy_type,
            drift_threshold=route.drift_threshold,
            metadata={**route.metadata, "reason": reason},
            strategy_params=strategy_params,
            sizing=sizing,
            expiry_date=expiry_date,
            order_sequence=route.order_sequence,
        )
        if carry_overnight:
            state_manager.update_many({"carry_overnight": True, "btst_reason": reason})
        state_manager.update_state("recenter_reason", reason if reason != "REGULAR" else "")
    return execution_success


def deploy_single_sniper_trade(index_symbol, expiry_date, reason="REGULAR"):
    logging.info("Deploying %s command-center strategy for %s...", reason, index_symbol)
    spot = get_spot_price(index_symbol)
    chain = get_option_chain(index_symbol, expiry_date) if spot else None
    if not spot or not chain:
        logging.critical("Cannot deploy %s trade: spot found=%s, chain found=%s.", reason, bool(spot), bool(chain))
        return False

    route = build_command_center_route(index_symbol, expiry_date, spot, chain)
    if not route or not route.legs:
        logging.critical(
            "Cannot deploy %s trade: command-center route failed/standby (%s).",
            reason,
            getattr(route, "no_trade_reason", ""),
        )
        return False
    return execute_command_center_route(index_symbol, expiry_date, spot, route, reason=reason)


def deploy_btst_momentum_trade(index_symbol, expiry_date, reason="EOD_MOMENTUM"):
    if not config.BTST_MOMENTUM_ENABLED:
        logging.info("BTST momentum module disabled by config.")
        return False

    now = datetime.now()
    if not is_btst_momentum_time(now):
        logging.info("BTST momentum check skipped: current time is %s.", now.strftime("%H:%M:%S"))
        return False

    if state_manager.load_state() and state_manager.load_state().get("active"):
        logging.info("BTST momentum check skipped: an active position still exists.")
        return False

    spot = get_spot_price(index_symbol)
    chain = get_option_chain(index_symbol, expiry_date) if spot else None
    india_vix = get_india_vix()
    candles = get_intraday_candles(index_symbol, minutes=15) if spot else []
    profile = candles_market_profile(spot, candles)

    if not spot or not chain or india_vix is None or not profile or profile.get("ema_15m_20") is None:
        logging.critical(
            "BTST momentum aborted: spot=%s, chain=%s, vix=%s, profile=%s.",
            bool(spot),
            bool(chain),
            india_vix,
            bool(profile),
        )
        return False

    route = route_btst_momentum_strategy(
        index_symbol,
        spot,
        chain,
        india_vix,
        profile["ema_15m_20"],
        profile["daily_low"],
        profile["daily_high"],
    )
    if not route.legs:
        logging.critical("BTST momentum no-trade: %s (%s).", route.no_trade_reason, route.metadata)
        return False

    sizing = calculate_position_size(
        config.ENVIRONMENT,
        route.strategy_type,
        india_vix,
        config.VIRTUAL_CAPITAL,
        index_symbol,
        lot_multiple_for_index(index_symbol),
        route=route,
    )
    if sizing.get("status") != "APPROVED":
        logging.critical("BTST momentum rejected by position sizing: %s", sizing)
        return False

    entry_prices = sync_entry_prices_with_quotes(route.legs, route.entry_prices)
    execution_success = place_option_spread_basket(
        route.legs,
        index_symbol,
        entry_prices,
        route.strikes,
        route.order_sequence,
        route.strategy_type,
        spot_price=spot,
        carry_overnight=True,
        metadata={**route.metadata, **profile, "reason": reason},
        quantity=sizing["quantity"],
        sizing=sizing,
    )
    if execution_success:
        initialize_sniper_state(
            entry_prices,
            strategy_type=route.strategy_type,
            metadata={**route.metadata, **profile, "reason": reason},
            strategy_params=strategy_params_for_type(route.strategy_type),
            sizing=sizing,
            expiry_date=expiry_date,
            order_sequence=route.order_sequence,
        )
        state_manager.update_many({
            "btst_reason": reason,
            "carry_overnight": True,
            "cutoff_hour": 15,
            "cutoff_minute": 25,
        })
        logging.critical("BTST momentum trade deployed: %s.", route.strategy_type)
    return execution_success


def deploy_adaptive_btst_trade(index_symbol, expiry_date, reason="ADAPTIVE_BTST"):
    if not config.BTST_MOMENTUM_ENABLED:
        logging.info("Adaptive BTST skipped: BTST module disabled by config.")
        return False

    if state_manager.load_state() and state_manager.load_state().get("active"):
        logging.info("Adaptive BTST skipped: an active position still exists.")
        return False

    spot = get_spot_price(index_symbol)
    chain = get_option_chain(index_symbol, expiry_date) if spot else None
    india_vix = get_india_vix()
    if not spot or not chain or india_vix is None:
        logging.critical(
            "Adaptive BTST aborted: spot=%s, chain=%s, vix=%s.",
            bool(spot),
            bool(chain),
            india_vix,
        )
        return False

    route = route_adaptive_btst_strategy(
        index_symbol,
        expiry_date,
        spot,
        chain,
        india_vix,
    )
    if not route.legs:
        logging.critical("Adaptive BTST no-trade: %s", route.no_trade_reason)
        return False

    logging.critical(
        "Adaptive BTST selected %s for %s.",
        route.strategy_type,
        index_symbol,
    )
    return execute_command_center_route(
        index_symbol,
        expiry_date,
        spot,
        route,
        reason=reason,
        carry_overnight=True,
    )


def scheduled_btst_momentum_check():
    if state_manager.load_state() and state_manager.load_state().get("active"):
        logging.info("Scheduled BTST momentum check skipped because an active position exists.")
        return False

    nifty_expiry, sensex_expiry = current_expiries()
    index_symbol, expiry_date, _, _ = session_for_time(datetime.now(), nifty_expiry, sensex_expiry)
    return deploy_adaptive_btst_trade(index_symbol, expiry_date, reason="SCHEDULED_1525")


def sync_open_position_after_reconnect(active_state):
    active_legs = (active_state or {}).get("legs", {})
    if not active_legs:
        return active_legs
    quotes = get_fresh_option_quotes(list(active_legs.values()))
    if quotes:
        last_live_prices = {
            leg_name: float(quotes.get(token, 0.0) or 0.0)
            for leg_name, token in active_legs.items()
            if float(quotes.get(token, 0.0) or 0.0) > 0
        }
        if last_live_prices:
            state_manager.update_many({
                "last_live_prices": last_live_prices,
                "feed_status": "RECONNECTED_REST_SYNC",
            })
    return active_legs


def get_stock_expiry_date(now=None):
    from datetime import datetime
    import calendar
    if now is None:
        now = datetime.now()
    year = now.year
    month = now.month
    
    def last_thursday(y, m):
        c = calendar.monthcalendar(y, m)
        thursdays = []
        for week in c:
            if week[3] != 0:
                thursdays.append(week[3])
        last_day = thursdays[-1]
        return datetime(y, m, last_day)
        
    last_thurs_curr = last_thursday(year, month)
    if now.date() > last_thurs_curr.date() or (now.date() == last_thurs_curr.date() and now.time() >= datetime.strptime("15:30", "%H:%M").time()):
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
        last_thurs_curr = last_thursday(year, month)
        
    return last_thurs_curr.strftime("%Y-%m-%d")


def get_symbol_expiry_date(symbol, now=None):
    symbol = str(symbol).upper().strip()
    if symbol == "NIFTY":
        return config._setting("NIFTY_EXPIRY")
    elif symbol == "SENSEX":
        return config._setting("SENSEX_EXPIRY")
    elif symbol == "BANKNIFTY":
        return config._setting("BANKNIFTY_EXPIRY") or config._setting("NIFTY_EXPIRY")
    else:
        return get_stock_expiry_date(now)


def _credit_sweep_route(symbol, direction, spot_price, option_chain):
    import credit_sweep
    from btst_vix_router import (
        STRATEGY_BTST_BEAR_CALL_CREDIT,
        STRATEGY_BTST_BULL_PUT_CREDIT,
        calculate_matrix_spread_legs,
    )

    strategy_type = STRATEGY_BTST_BULL_PUT_CREDIT if direction == "BULLISH" else STRATEGY_BTST_BEAR_CALL_CREDIT
    route = calculate_matrix_spread_legs(symbol, spot_price, option_chain, strategy_type)
    if route and not route.no_trade_reason:
        route.strategy_type = credit_sweep.strategy_type_for_direction(direction)
        route.carry_overnight = False
        route.metadata.update({"paper_only": bool(config.CREDIT_SWEEP_PAPER_ONLY), "source": "CREDIT_SWEEP"})
    return route


def _update_credit_sweep_paper_position(now=None):
    import credit_sweep
    import data_feed

    now = now or datetime.now()
    payload = credit_sweep.load_credit_sweep_state()
    position = payload.get("position") or {}
    if not (payload.get("active") and position):
        return payload

    symbol = str(position.get("symbol", "")).upper()
    fresh_spot = get_spot_price(symbol)
    if not fresh_spot:
        position["reject_reason"] = "Could not refresh spot for paper MTM"
        payload["position"] = position
        credit_sweep.save_credit_sweep_state(payload)
        return payload

    current_prices = None
    legs = position.get("legs") or {}
    if legs:
        try:
            quotes = data_feed.get_fresh_option_quotes(list(legs.values()))
            if quotes:
                current_prices = {
                    leg_name: float(quotes.get(token, 0.0) or 0.0)
                    for leg_name, token in legs.items()
                }
                if not all(value > 0 for value in current_prices.values()):
                    current_prices = None
        except Exception as err:
            logging.warning("Credit Sweep paper quote refresh failed: %s", err)

    mark = credit_sweep.paper_mark_to_market(position, fresh_spot, current_prices=current_prices)
    position.update(mark)
    if current_prices:
        position["last_live_prices"] = current_prices

    exit_reason = credit_sweep.paper_exit_reason(position, fresh_spot, now=now)
    if exit_reason:
        closed = credit_sweep.record_paper_exit(position, mark, exit_reason)
        history = payload.get("paper_trades", [])
        history.append(closed)
        payload.update({"active": False, "position": closed, "paper_trades": history})
        logging.critical("Credit Sweep paper trade closed: %s %s (%s)", symbol, exit_reason, mark)
    else:
        payload.update({"active": True, "position": position})

    credit_sweep.save_credit_sweep_state(payload)
    return payload


def scan_credit_sweep_and_paper_trades():
    import credit_sweep
    import data_feed

    now = datetime.now()
    payload = _update_credit_sweep_paper_position(now)
    scanner_rows = []
    best_candidate = None
    best_score = -1

    if not config.CREDIT_SWEEP_ENABLED:
        payload["scanner"] = [{"status": "DISABLED", "reject_reason": "Credit Sweep disabled", "updated_at": now.strftime("%H:%M:%S")}]
        credit_sweep.save_credit_sweep_state(payload)
        return

    active_paper = bool(payload.get("active") and payload.get("position", {}).get("active", True))
    from_date = now - timedelta(days=7)

    for symbol in config.CREDIT_SWEEP_SYMBOLS:
        symbol = str(symbol).strip().upper()
        try:
            candles = data_feed.get_broker().get_intraday_candles(symbol, minutes=5, from_date=from_date)
            df = credit_sweep.normalize_candles(candles)
            today = now.strftime("%Y-%m-%d")
            day = df[df["date"] == today].copy() if not df.empty else df
            levels = credit_sweep.prior_day_levels(df, today) if not df.empty else None
            signal = credit_sweep.evaluate_credit_sweep_signal(symbol, day, levels, now=now, interval_minutes=5)
            row = signal.to_row({"updated_at": now.strftime("%H:%M:%S")})

            if signal.confirmed:
                if active_paper:
                    row.update({"status": "REJECTED", "reject_reason": "Credit Sweep paper trade already active"})
                elif credit_sweep.has_credit_sweep_entry_today(symbol, now=now):
                    row.update({"status": "REJECTED", "reject_reason": "Credit Sweep already entered this symbol today"})
                else:
                    fresh_spot = get_spot_price(symbol)
                    distance_ok, distance_reason = credit_sweep.validate_live_price_distance(signal, fresh_spot or 0.0)
                    row["fresh_spot"] = round(float(fresh_spot or 0.0), 4)
                    if not distance_ok:
                        row.update({"status": "REJECTED", "reject_reason": distance_reason})
                    else:
                        expiry_date = get_symbol_expiry_date(symbol, now)
                        chain = data_feed.get_broker().get_option_chain(symbol, expiry_date)
                        route = _credit_sweep_route(symbol, signal.direction, fresh_spot, chain)
                        route_ok, route_reason, metrics = credit_sweep.validate_credit_spread_route(route)
                        row.update({
                            "expiry": expiry_date,
                            "strategy_type": getattr(route, "strategy_type", "") if route else "",
                            "net_credit": metrics.get("net_credit", 0.0),
                            "spread_width": metrics.get("spread_width", 0.0),
                            "defined_loss": metrics.get("defined_loss", 0.0),
                            "planned_legs": getattr(route, "strikes", {}) if route else {},
                        })
                        if route_ok:
                            if signal.score > best_score:
                                best_score = signal.score
                                best_candidate = {
                                    "signal": signal,
                                    "route": route,
                                    "metrics": metrics,
                                    "fresh_spot": fresh_spot,
                                }
                        else:
                            row.update({"status": "REJECTED", "reject_reason": route_reason})

            scanner_rows.append(row)
        except Exception as err:
            logging.error("Credit Sweep scanner failed for %s: %s", symbol, err)
            scanner_rows.append({
                "symbol": symbol,
                "status": "ERROR",
                "reject_reason": str(err),
                "updated_at": now.strftime("%H:%M:%S"),
            })

    active_state = state_manager.load_state()
    active_live = bool(active_state and active_state.get("active"))

    if best_candidate:
        signal = best_candidate["signal"]
        if config.CREDIT_SWEEP_PAPER_ONLY:
            if not active_paper:
                position = credit_sweep.record_paper_entry(
                    signal,
                    best_candidate["route"],
                    best_candidate["metrics"],
                    best_candidate["fresh_spot"],
                )
                payload.update({"active": True, "position": position})
                logging.critical("Credit Sweep paper setup deployed: %s", position)
                for row in scanner_rows:
                    if row.get("symbol") == signal.symbol:
                        row["status"] = "PAPER_ENTRY"
                        row["reject_reason"] = "Paper trade created"
        else:
            if not active_live:
                import math
                import execution
                lot_size = config.get_symbol_lot_size(signal.symbol)
                risk_per_lot = (best_candidate["metrics"]["spread_width"] - best_candidate["metrics"]["net_credit"]) * lot_size
                lots = 1
                if config.CREDIT_SWEEP_RISK_BUDGET > 0:
                    lots = math.floor(config.CREDIT_SWEEP_RISK_BUDGET / risk_per_lot)
                    if lots < 1:
                        lots = 1
                trade_quantity = lots * lot_size
                
                metadata = {
                    "setup_score": signal.score,
                    "direction": signal.direction,
                    "stop_loss_pts": abs(best_candidate["fresh_spot"] - signal.stop_price),
                    "target_pts": abs(signal.target_price - best_candidate["fresh_spot"]),
                    "entry_spot": best_candidate["fresh_spot"],
                    "source": "CREDIT_SWEEP",
                    "paper_only": False,
                }
                
                logging.critical("Executing LIVE Credit Sweep entry for %s (%s) with %s lots (qty=%s)", signal.symbol, signal.direction, lots, trade_quantity)
                success = execution.place_option_spread_basket(
                    legs=best_candidate["route"].legs,
                    index_symbol=signal.symbol,
                    entry_prices=best_candidate["route"].entry_prices,
                    strikes=best_candidate["route"].strikes,
                    order_sequence=best_candidate["route"].order_sequence,
                    strategy_type=credit_sweep.strategy_type_for_direction(signal.direction),
                    spot_price=best_candidate["fresh_spot"],
                    carry_overnight=False,
                    metadata=metadata,
                    quantity=trade_quantity,
                    sizing={"lots_to_deploy": lots, "lot_multiple": lot_size, "capital_deployed": lots * risk_per_lot}
                )
                if success:
                    logging.critical("Successfully deployed LIVE Credit Sweep trade on %s! Starting background risk monitor...", signal.symbol)
                    import threading
                    
                    def run_monitor():
                        try:
                            exit_reason, exit_prices = monitor_with_reconnects(best_candidate["route"].legs, signal.symbol)
                            logging.critical("LIVE Credit Sweep trade on %s exited due to %s. Squaring off basket...", signal.symbol, exit_reason)
                            execution.square_off_all(exit_prices, exit_reason=exit_reason)
                        except Exception as e:
                            logging.error("LIVE Credit Sweep monitor exception: %s", e)
                            
                    threading.Thread(target=run_monitor, daemon=True).start()
                    
                    for row in scanner_rows:
                        if row.get("symbol") == signal.symbol:
                            row["status"] = "LIVE_ENTRY"
                            row["reject_reason"] = "Live trade deployed"

    payload["scanner"] = scanner_rows
    credit_sweep.save_credit_sweep_state(payload)



def scan_market_and_execute_trades():
    import json
    import os
    import pandas as pd
    import math
    import backtest_orderblock_mitigation as btom
    import data_feed
    import strategy
    import execution
    
    active_state = state_manager.load_state()
    if active_state and active_state.get("active"):
        logging.info("Active trade exists. Scanner will only update screener_state.json.")
        
    symbols = config.MTF_SCREENER_SYMBOLS
    screener_results = []
    
    params = btom.BacktestParams(
        pivot_len=config.MTF_PIVOT_LEN,
        displacement_multiplier=config.MTF_DISPLACEMENT_MULTIPLIER,
        invalidation_buffer=config.MTF_INVALIDATION_BUFFER,
        max_leg_age=config.MTF_MAX_LEG_AGE,
        target_rr=config.MTF_TARGET_RR,
        trigger_type=config.MTF_TRIGGER_TYPE,
        stop_loss_type=config.MTF_STOP_LOSS_TYPE,
        use_vwap_filter=config.MTF_USE_VWAP_FILTER,
        max_trades_per_day=config.MTF_MAX_TRADES_PER_DAY,
    )
    
    now = datetime.now()
    from_date_5m = now - timedelta(days=5)
    
    best_candidate = None
    best_score = -1
    
    for symbol in symbols:
        symbol = symbol.strip().upper()
        try:
            # 5m candles (spaced to prevent rate limit 429)
            time.sleep(2.0)
            candles_5m = data_feed.get_broker().get_intraday_candles(symbol, minutes=5, from_date=from_date_5m)
            if not candles_5m:
                logging.debug("No 5m candles returned for %s", symbol)
                continue
                
            df_5m = pd.DataFrame(candles_5m, columns=["datetime", "open", "high", "low", "close", "volume", "oi"])
            df_5m["dt"] = pd.to_datetime(df_5m["datetime"], unit="s").dt.tz_localize("UTC").dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
            df_5m["date"] = df_5m["dt"].dt.date.astype(str)
            df_5m["time"] = df_5m["dt"].dt.strftime("%H:%M")
            df_5m["atr14"] = (df_5m["high"] - df_5m["low"]).rolling(14).mean().fillna(1.0)
            
            # Calculate daily levels from df_5m (shifted by 1 day)
            daily_levels = df_5m.groupby("date").agg(
                pd_high=("high", "max"),
                pd_low=("low", "min"),
                pd_close=("close", "last")
            ).shift(1)
            
            # 1m candles for today (spaced to prevent rate limit 429)
            time.sleep(2.0)
            from_date_1m = now.replace(hour=9, minute=15, second=0, microsecond=0)
            candles_1m = data_feed.get_broker().get_intraday_candles(symbol, minutes=1, from_date=from_date_1m)
            if not candles_1m:
                logging.debug("No 1m candles returned for %s", symbol)
                continue
            df_1m = pd.DataFrame(candles_1m, columns=["datetime", "open", "high", "low", "close", "volume", "oi"])
            df_1m["dt"] = pd.to_datetime(df_1m["datetime"], unit="s").dt.tz_localize("UTC").dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
            df_1m["date"] = df_1m["dt"].dt.date.astype(str)
            df_1m["time"] = df_1m["dt"].dt.strftime("%H:%M")
            
            # Merge daily levels into df_1m
            df_1m = df_1m.merge(daily_levels, on="date", how="left")
            
            typical = (df_1m["high"] + df_1m["low"] + df_1m["close"]) / 3.0
            df_1m["pv"] = typical * df_1m["volume"].clip(lower=1.0)
            df_1m["vwap"] = df_1m["pv"].cumsum() / df_1m["volume"].clip(lower=1.0).cumsum()
            df_1m["volume_median20"] = df_1m["volume"].rolling(20, min_periods=5).median().fillna(1.0)
            df_1m["atr14"] = (df_1m["high"] - df_1m["low"]).rolling(14).mean().fillna(1.0)
            
            df_state = btom.run_5m_ob_tracker(df_5m, params)
            bull_trig, bear_trig = btom.compute_1m_triggers(df_1m, params.trigger_type)
            df_1m["bull_trigger"] = bull_trig
            df_1m["bear_trigger"] = bear_trig
            
            merged = pd.merge_asof(df_1m, df_state, on="dt", direction="backward")
            if len(merged) < 2:
                continue
                
            # Closed candle check (ensure candle is fully closed; revert to -2 if -1 is still forming)
            last_idx = -1
            dt_val = merged.iloc[last_idx]["dt"]
            dt_close = dt_val + timedelta(minutes=1)
            if now < dt_close:
                if len(merged) >= 2:
                    last_idx = -2
                    
            curr_price = float(merged.iloc[last_idx]["close"])
            trend_5m = int(merged.iloc[last_idx]["trend"])
            ob_low = float(merged.iloc[last_idx]["ob_low"])
            ob_high = float(merged.iloc[last_idx]["ob_high"])
            zone_entry = float(merged.iloc[last_idx]["zone_entry_price"])
            stop_5m = float(merged.iloc[last_idx]["stop_loss_5m"])
            target_5m = float(merged.iloc[last_idx]["take_profit_5m"])
            vwap = float(merged.iloc[last_idx]["vwap"])
            zone_time = str(merged.iloc[last_idx]["ob_time"]) if "ob_time" in merged.columns else ""
            
            low_price = float(merged.iloc[last_idx]["low"])
            high_price = float(merged.iloc[last_idx]["high"])
            
            pullback_pending = False
            pending_direction = None
            
            # Find the last exit time for this symbol today to avoid old pullbacks
            last_exit_time = None
            csv_path = os.path.join(BASE_DIR, "sandbox_trade_logs.csv")
            if os.path.exists(csv_path):
                try:
                    df_ledger = pd.read_csv(csv_path)
                    if not df_ledger.empty and "Timestamp" in df_ledger.columns and "Index" in df_ledger.columns and "Action" in df_ledger.columns:
                        df_ledger["Timestamp_dt"] = pd.to_datetime(df_ledger["Timestamp"], errors="coerce")
                        today_str = now.strftime("%Y-%m-%d")
                        symbol_exits = df_ledger[
                            (df_ledger["Timestamp_dt"].dt.date.astype(str) == today_str) &
                            (df_ledger["Index"].astype(str).str.upper() == symbol) &
                            (df_ledger["Action"] == "EXIT")
                        ]
                        if not symbol_exits.empty:
                            last_exit_time = symbol_exits["Timestamp_dt"].max()
                except Exception as e:
                    logging.error("Failed to parse ledger for last exit of %s: %s", symbol, e)

            # Historical simulation of pullback within the current active OB trend cycle
            if trend_5m != 0 and zone_time != "":
                # Filter merged to only include rows belonging to the current OB cycle
                df_cycle = merged[(merged["ob_time"] == zone_time) & (merged["trend"] == trend_5m)]
                if last_exit_time is not None:
                    # Filter to candles after the last exit
                    df_cycle = df_cycle[df_cycle["dt"] > last_exit_time]
                
                # Check if there is an active trade for this symbol to disable new setups
                is_symbol_active = active_state and active_state.get("active") and str(active_state.get("symbol")).upper() == symbol
                
                if not is_symbol_active and not df_cycle.empty:
                    for _, row in df_cycle.iterrows():
                        # A. Check invalidation first if already pending
                        if pullback_pending:
                            close_val = float(row["close"])
                            if pending_direction == "BULLISH":
                                if close_val < ob_low:
                                    pullback_pending = False
                            else:  # BEARISH
                                if close_val > ob_high:
                                    pullback_pending = False
                        
                        # B. Check for pullback touch to activate pending state
                        if not pullback_pending:
                            low_val = float(row["low"])
                            high_val = float(row["high"])
                            close_val = float(row["close"])
                            if trend_5m == 1:
                                if low_val <= zone_entry and close_val >= ob_low:
                                    pullback_pending = True
                                    pending_direction = "BULLISH"
                            elif trend_5m == -1:
                                if high_val >= zone_entry and close_val <= ob_high:
                                    pullback_pending = True
                                    pending_direction = "BEARISH"
                                    
            trigger_active = False
            score = 70
            
            # Daily levels for confluence scoring
            pd_low = float(merged.iloc[last_idx]["pd_low"]) if "pd_low" in merged.columns else float('nan')
            pd_high = float(merged.iloc[last_idx]["pd_high"]) if "pd_high" in merged.columns else float('nan')
            
            if pullback_pending:
                # 1. Daily Level Confluence Scoring
                if pending_direction == "BULLISH":
                    if not pd.isna(pd_low) and low_price <= pd_low:
                        score += 10
                else:
                    if not pd.isna(pd_high) and high_price >= pd_high:
                        score += 10
                        
                if pending_direction == "BULLISH":
                    if merged.iloc[last_idx]["bull_trigger"]:
                        if not params.use_vwap_filter or curr_price > vwap:
                            trigger_active = True
                            if curr_price > vwap:
                                score += 10
                            if merged.iloc[last_idx]["volume"] >= merged.iloc[last_idx]["volume_median20"] * 1.15:
                                score += 10
                else:
                    if merged.iloc[last_idx]["bear_trigger"]:
                        if not params.use_vwap_filter or curr_price < vwap:
                            trigger_active = True
                            if curr_price < vwap:
                                score += 10
                            if merged.iloc[last_idx]["volume"] >= merged.iloc[last_idx]["volume_median20"] * 1.15:
                                score += 10
                                
            # Signal quality gate validation and age calculation
            dt_val_actual = merged.iloc[last_idx]["dt"]
            dt_close_actual = dt_val_actual + timedelta(minutes=1)
            signal_age = max(0.0, (now - dt_close_actual).total_seconds())
            
            is_valid = False
            reject_reason = ""
            remaining_rr = 0.0
            
            if trigger_active:
                fresh_spot = get_spot_price(symbol)
                if fresh_spot:
                    is_valid, reject_reason, remaining_rr = btom.validate_mtf_signal(
                        symbol=symbol,
                        time_str=merged.iloc[last_idx]["time"],
                        score=score,
                        trend=trend_5m,
                        curr_price=curr_price,
                        stop_loss=stop_5m,
                        target=target_5m,
                        atr_1m=merged.iloc[last_idx]["atr14"],
                        pd_low=pd_low,
                        pd_high=pd_high,
                        ob_low=ob_low,
                        ob_high=ob_high,
                        zone_entry=zone_entry,
                        is_live=True,
                        signal_dt=dt_val_actual,
                        now=now,
                        fresh_spot=fresh_spot,
                    )
                else:
                    reject_reason = "No fresh spot price available"
            else:
                reject_reason = "No trigger confirmed" if pullback_pending else "No pending pullback"

            screener_results.append({
                "symbol": symbol,
                "price": curr_price,
                "trend": "BULLISH" if trend_5m == 1 else "BEARISH" if trend_5m == -1 else "NEUTRAL",
                "zone_time": zone_time,
                "zone_entry": zone_entry,
                "stop_loss": stop_5m,
                "target": target_5m,
                "pullback": "PENDING" if pullback_pending else "NONE",
                "trigger": "CONFIRMED" if (trigger_active and is_valid) else "REJECTED" if (trigger_active and not is_valid) else "NONE",
                "score": score if pullback_pending else 0,
                "live_rr": round(remaining_rr, 2) if trigger_active else 0.0,
                "signal_age": int(signal_age),
                "reject_reason": reject_reason if (trigger_active and not is_valid) else "NONE" if (trigger_active and is_valid) else "",
                "updated_at": now.strftime("%H:%M:%S")
            })
            
            if trigger_active and is_valid and not (active_state and active_state.get("active")):
                trades_today = 0
                csv_path = os.path.join(BASE_DIR, "sandbox_trade_logs.csv")
                if os.path.exists(csv_path):
                    try:
                        df_ledger = pd.read_csv(csv_path)
                        df_ledger["Date"] = pd.to_datetime(df_ledger["Timestamp"]).dt.date.astype(str)
                        today_str = now.strftime("%Y-%m-%d")
                        trades_today = len(df_ledger[(df_ledger["Date"] == today_str) & (df_ledger["Action"] == "ENTRY")])
                    except Exception:
                        pass
                        
                if trades_today < params.max_trades_per_day:
                    if score > best_score:
                        best_score = score
                        best_candidate = {
                            "symbol": symbol,
                            "direction": pending_direction,
                            "entry_price": curr_price,
                            "stop_loss": stop_5m,
                            "target": target_5m,
                            "score": score,
                            "time_str": merged.iloc[last_idx]["time"],
                            "atr_1m": merged.iloc[last_idx]["atr14"],
                            "pd_low": pd_low,
                            "pd_high": pd_high,
                            "ob_low": ob_low,
                            "ob_high": ob_high,
                            "zone_entry": zone_entry,
                            "signal_dt": dt_val_actual,
                        }
        except Exception as e:
            logging.error("Scanner failed for symbol %s: %s", symbol, e)
            
    screener_file = os.path.join(BASE_DIR, "screener_state.json")
    state_manager._atomic_write(screener_file, screener_results)
    
    if best_candidate and not (active_state and active_state.get("active")):
        logging.critical("🎯 SCANNER FOUND CONFIRMED SETUP: %s", best_candidate)
        try:
            deploy_mtf_oblt_trade(best_candidate)
        except Exception as deploy_err:
            logging.error("Failed to deploy MTF-OBLT trade: %s", deploy_err)


def deploy_mtf_oblt_trade(candidate):
    import data_feed
    import strategy
    import execution
    import state_manager
    import config
    import math
    from backtest_orderblock_mitigation import get_strike_step, get_margin_per_lot
    import backtest_orderblock_mitigation as btom
    
    symbol = candidate["symbol"]
    direction = candidate["direction"]
    entry_price = candidate["entry_price"]
    stop_loss = candidate["stop_loss"]
    target = candidate["target"]
    score = candidate["score"]
    
    # Fetch fresh spot price from broker immediately before building legs
    fresh_spot = get_spot_price(symbol)
    if not fresh_spot:
        logging.error("Failed to recheck fresh spot price immediately before building option legs for %s", symbol)
        return
        
    is_valid, reject_reason, remaining_rr = btom.validate_mtf_signal(
        symbol=symbol,
        time_str=candidate["time_str"],
        score=score,
        trend=1 if direction == "BULLISH" else -1,
        curr_price=entry_price,
        stop_loss=stop_loss,
        target=target,
        atr_1m=candidate["atr_1m"],
        pd_low=candidate["pd_low"],
        pd_high=candidate["pd_high"],
        ob_low=candidate["ob_low"],
        ob_high=candidate["ob_high"],
        zone_entry=candidate["zone_entry"],
        is_live=True,
        signal_dt=candidate["signal_dt"],
        now=datetime.now(),
        fresh_spot=fresh_spot,
    )
    
    if not is_valid:
        logging.critical("❌ Pre-execution recheck failed for %s: %s (fresh spot: %.2f)", symbol, reject_reason, fresh_spot)
        return
        
    # Use the immediate fresh spot for legs builder
    entry_price = fresh_spot
    
    expiry_date = get_symbol_expiry_date(symbol)
    chain = data_feed.get_broker().get_option_chain(symbol, expiry_date)
    if not chain:
        logging.error("Failed to fetch option chain for %s on %s", symbol, expiry_date)
        return
        
    strategy_type = config.MTF_STRATEGY_TYPE
    if strategy_type == "Ratio":
        legs, entry_prices, strikes = strategy.calculate_ratio_spread_legs(symbol, entry_price, chain, direction)
        order_sequence = [
            ("buy_ce" if direction == "BULLISH" else "buy_pe", "BUY"),
            ("sell_ce" if direction == "BULLISH" else "sell_pe", "SELL")
        ]
    elif strategy_type == "Synthetic Future":
        legs, entry_prices, strikes = strategy.calculate_synthetic_future_legs(symbol, entry_price, chain, direction)
        order_sequence = [
            ("buy_ce" if direction == "BULLISH" else "buy_pe", "BUY"),
            ("sell_pe" if direction == "BULLISH" else "sell_ce", "SELL")
        ]
    else:
        logging.error("Unsupported live strategy type for MTF-OBLT: %s", strategy_type)
        return
        
    if not legs:
        logging.error("Failed to build option legs for %s", symbol)
        return
        
    lot_size = config.get_symbol_lot_size(symbol)
    margin_per_lot = get_margin_per_lot(symbol, entry_price)
    
    strike_step = get_strike_step(symbol)
    risk_per_lot = 0.20 * strike_step * lot_size if strategy_type == "Ratio" else abs(entry_price - stop_loss) * lot_size
    
    capital = float(config._setting("VIRTUAL_CAPITAL", 200000.0))
    risk_budget = float(config._setting("MTF_DISCRETE_RISK_BUDGET", 1000.0))
    
    lots_by_risk = math.floor(risk_budget / risk_per_lot) if risk_per_lot > 0 else 1
    lots_by_margin = math.floor(capital / margin_per_lot) if margin_per_lot > 0 else 1
    
    lots = min(lots_by_risk, lots_by_margin)
    if lots < 1:
        lots = 1
        
    trade_quantity = lots * lot_size
    
    metadata = {
        "setup_score": score,
        "direction": direction,
        "stop_loss_pts": abs(entry_price - stop_loss),
        "target_pts": abs(target - entry_price),
        "entry_spot": entry_price,
    }
    
    logging.critical("Executing live MTF-OBLT entry for %s (%s) with %s lots (qty=%s)", symbol, direction, lots, trade_quantity)
    success = execution.place_option_spread_basket(
        legs=legs,
        index_symbol=symbol,
        entry_prices=entry_prices,
        strikes=strikes,
        order_sequence=order_sequence,
        strategy_type=strategy_type,
        spot_price=entry_price,
        carry_overnight=False,
        metadata=metadata,
        quantity=trade_quantity,
        sizing={"lots_to_deploy": lots, "lot_multiple": lot_size, "capital_deployed": lots * margin_per_lot}
    )
    
    if success:
        logging.critical("Successfully deployed MTF-OBLT trade on %s! Starting background risk monitor...", symbol)
        import threading
        
        def run_monitor():
            try:
                exit_reason, exit_prices = monitor_with_reconnects(legs, symbol)
                logging.critical("Background trade exited. Exit reason: %s", exit_reason)
                execution.square_off_all(exit_prices, exit_reason=exit_reason)
            except Exception as monitor_err:
                logging.error("Exception in background risk monitor thread: %s", monitor_err)
                
        monitor_thread = threading.Thread(target=run_monitor, name=f"risk-monitor-{symbol}", daemon=True)
        monitor_thread.start()


def trigger_manual_entry():
    logging.critical("Triggering manual entry from command center...")
    symbols = config.MTF_SCREENER_SYMBOLS
    if not symbols:
        return
        
    symbol = symbols[0].strip().upper()
    try:
        spot = get_spot_price(symbol)
        expiry_date = get_symbol_expiry_date(symbol)
        chain = data_feed.get_broker().get_option_chain(symbol, expiry_date)
        if not spot or not chain:
            logging.error("Manual entry failed: cannot fetch spot or chain.")
            return
            
        candidate = {
            "symbol": symbol,
            "direction": "BULLISH",
            "entry_price": spot,
            "stop_loss": spot - 50.0,
            "target": spot + 150.0,
            "score": 100
        }
        deploy_mtf_oblt_trade(candidate)
    except Exception as e:
        logging.error("Manual entry deployment failed: %s", e)


def monitor_with_reconnects(legs, index_symbol):
    reconnects = 0
    backoff_seconds = list(config.WEBSOCKET_RECONNECT_BACKOFF_SECONDS)
    while True:
        write_heartbeat("MONITORING")
        stop_loss_hit, exit_prices = monitor_live_prices(legs, risk_management_evaluator)
        if stop_loss_hit != "SOCKET_DEAD":
            return stop_loss_hit, exit_prices

        reconnects += 1
        state_manager.update_state("socket_reconnects", reconnects)
        logging.critical(
            "WebSocket died for %s; reconnect attempt %s/%s.",
            index_symbol,
            reconnects,
            len(backoff_seconds),
        )

        active_state = state_manager.load_state()
        if reconnects <= len(backoff_seconds) and active_state and active_state.get("active"):
            sleep_seconds = float(backoff_seconds[reconnects - 1])
            logging.info("Reconnecting WebSocket after %.1fs backoff.", sleep_seconds)
            time.sleep(sleep_seconds)
            active_state = state_manager.load_state()
            if active_state and active_state.get("active"):
                legs = sync_open_position_after_reconnect(active_state) or legs
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

    if fresh_entry_cutoff_reached():
        logging.critical(
            "%s re-entry blocked: fresh entry cutoff %s has passed.",
            exit_reason,
            config.FRESH_ENTRY_CUTOFF_TIME,
        )
        return False

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


def is_dhan_token_valid(token):
    if not token or len(token.split('.')) < 2:
        return False
    try:
        import base64
        import json
        import time
        
        payload_b64 = token.split('.')[1]
        padding = len(payload_b64) % 4
        if padding > 0:
            payload_b64 += "=" * (4 - padding)
            
        payload_json = base64.b64decode(payload_b64).decode('utf-8')
        payload = json.loads(payload_json)
        
        exp = payload.get("exp")
        if exp:
            return float(exp) > time.time() + 43200
    except Exception:
        pass
    return False


def auto_authenticate_dhan():
    if config.get_active_broker() != "DHAN":
        return
    
    current_token = config.get_dhan_access_token()
    if is_dhan_token_valid(current_token):
        logging.info("Dhan TOTP auto-auth: Current saved access token is still valid. Skipping token generation.")
        return
        
    client_id = config.get_dhan_client_id()
    pin = config._setting("DHAN_PIN", "")
    totp_secret = config._setting("DHAN_TOTP_SECRET", "")
    
    if not client_id or not pin or not totp_secret:
        logging.info("Dhan TOTP auto-auth skipped: missing Client ID, PIN, or TOTP Secret in settings.")
        return
        
    logging.info("Dhan TOTP auto-auth: Attempting programmatic token generation...")
    from auth import generate_dhan_token_with_totp
    token = generate_dhan_token_with_totp(client_id, pin, totp_secret)
    if token:
        try:
            settings_path = os.path.join(BASE_DIR, "settings.json")
            if os.path.exists(settings_path):
                with open(settings_path, "r") as f:
                    settings = json.load(f)
            else:
                settings = {}
            settings["DHAN_ACCESS_TOKEN"] = token
            
            temp_path = settings_path + ".tmp"
            with open(temp_path, "w") as f:
                json.dump(settings, f, indent=4)
            for _ in range(5):
                try:
                    os.replace(temp_path, settings_path)
                    break
                except PermissionError:
                    time.sleep(0.05)
            logging.info("Dhan TOTP auto-auth: Programmatic token successfully updated & saved to settings.json!")
        except Exception as e:
            logging.error("Dhan TOTP auto-auth: Failed to save token: %s", e)
    else:
        logging.error("Dhan TOTP auto-auth: Programmatic token generation failed.")


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

    logging.info("Scheduling Credit Sweep scanner running every 1 minute.")
    schedule.every(1).minutes.do(scan_credit_sweep_and_paper_trades).tag('trading_jobs')


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
    logging.info(f"  Entry Standby Gate: fresh entries start at {config.ENTRY_STANDBY_TIME}")
    logging.info("  Expiry Day Strategy: 0-DTE chains skipped; next weekly expiry used")
    logging.info(f"  Weekend Guard: ON")

    calendar_invalid = calendar_blocks_trading()
    nifty_expiry, sensex_expiry = current_expiries()

    if not calendar_invalid:
        auto_authenticate_dhan()
        build_todays_schedule()
        schedule.every().day.at("08:00").do(auto_authenticate_dhan)
        schedule.every().day.at("08:00").do(build_todays_schedule)

    recovered_state = state_manager.load_state()

    if recovered_state and recovered_state.get("active"):
        rec_symbol = recovered_state.get("index_symbol", "UNKNOWN")
        logging.critical(f"🔄 ORPHANED TRADE DETECTED ON BOOT! Instantly recovering {rec_symbol} session...")
        try:
            legs = recovered_state.get("legs")
            if legs:
                exit_reason, exit_prices = monitor_with_reconnects(legs, rec_symbol)
                logging.critical("Recovered trade exited. Exit reason: %s", exit_reason)
                execution.square_off_all(exit_prices, exit_reason=exit_reason)
        except Exception as e:
            logging.critical("Failed to recover orphaned trade: %s", e)
    
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    eod_cutoff = datetime.strptime("15:25", "%H:%M").time()
    fresh_entry_cutoff = datetime.strptime(config.FRESH_ENTRY_CUTOFF_TIME, "%H:%M").time()
    morning_start = datetime.strptime(config.ENTRY_STANDBY_TIME, "%H:%M").time()
    
    if not calendar_invalid and not (hasattr(config, 'MARKET_HOLIDAYS') and today_str in config.MARKET_HOLIDAYS) and now.weekday() < 5:
        current_time = now.time()
        if morning_start <= current_time < fresh_entry_cutoff:
            logging.critical("🏃 LATE BOOT DETECTED! Running Credit Sweep scanner immediately on startup...")
            try:
                scan_credit_sweep_and_paper_trades()
            except Exception as e:
                logging.error("Startup scan failed: %s", e)

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
            if calendar_invalid:
                logging.critical("Manual entry ignored because expiry calendar is invalid.")
            else:
                trigger_manual_entry()
            
        time.sleep(1)
