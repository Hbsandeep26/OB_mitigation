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
    previous_flow_snapshot = None

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