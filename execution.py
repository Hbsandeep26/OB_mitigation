import logging
import time

from broker import get_broker
import config
import upstox_client
from logger import log_trade
from notifier import send_telegram_alert
from upstox_client.rest import ApiException
import state_manager


class OrderConfirmationError(Exception):
    pass


class PartialFillError(OrderConfirmationError):
    def __init__(self, message, fill_info):
        super().__init__(message)
        self.fill_info = fill_info


COMPLETE_STATUSES = {"COMPLETE", "COMPLETED", "FILLED"}
FAILED_STATUSES = {"REJECTED", "CANCELLED", "CANCELED"}


def validate_trade_quantity(index_symbol, quantity):
    lot_multiple = config.NIFTY_LOT_MULTIPLE if index_symbol == "NIFTY" else config.SENSEX_LOT_MULTIPLE
    quantity = int(quantity)
    if quantity <= 0:
        raise ValueError(f"{index_symbol} quantity must be positive. Got {quantity}.")
    if quantity % lot_multiple != 0:
        raise ValueError(f"{index_symbol} quantity {quantity} is not a multiple of lot size {lot_multiple}.")
    return quantity


def _make_order_apis():
    return get_broker().make_order_apis()


def _field(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _extract_order_ids(response):
    data = _field(response, "data", {})
    order_ids = _field(data, "order_ids", None)
    if order_ids:
        return list(order_ids)
    order_id = _field(data, "order_id", None) or _field(response, "order_id", None)
    return [order_id] if order_id else []


def _extract_order_data(response):
    data = _field(response, "data", None)
    if isinstance(data, list):
        return data[-1] if data else {}
    return data or {}


def _build_market_order(token, transaction_type, quantity, tag):
    return upstox_client.PlaceOrderV3Request(
        quantity=int(quantity),
        product="I",
        validity="DAY",
        price=0.0,
        tag=tag,
        instrument_token=token,
        order_type="MARKET",
        transaction_type=transaction_type,
        disclosed_quantity=0,
        trigger_price=0.0,
        is_amo=False,
        slice=True,
    )


def _fill_info(leg_name, token, transaction_type, order_id, order_data):
    return {
        "leg_name": leg_name,
        "token": token,
        "transaction_type": transaction_type,
        "order_id": order_id,
        "status": str(_field(order_data, "status", "")).upper(),
        "filled_quantity": int(_field(order_data, "filled_quantity", 0) or 0),
        "pending_quantity": int(_field(order_data, "pending_quantity", 0) or 0),
        "average_price": float(_field(order_data, "average_price", 0.0) or 0.0),
        "status_message": _field(order_data, "status_message", "") or _field(order_data, "status_message_raw", ""),
    }


def place_and_confirm(order_api_v3, order_api, request_body, leg_name, token, transaction_type):
    placement = order_api_v3.place_order(request_body)
    order_ids = _extract_order_ids(placement)
    if len(order_ids) != 1:
        raise OrderConfirmationError(f"{leg_name}: broker returned unexpected order ids: {order_ids}")

    order_id = order_ids[0]
    expected_qty = int(_field(request_body, "quantity", 0))
    deadline = time.time() + config.ORDER_CONFIRM_TIMEOUT_SECONDS
    last_info = None

    while time.time() < deadline:
        status_response = order_api.get_order_status(order_id=order_id)
        order_data = _extract_order_data(status_response)
        fill_info = _fill_info(leg_name, token, transaction_type, order_id, order_data)
        last_info = fill_info

        status = fill_info["status"]
        filled_qty = fill_info["filled_quantity"]

        if status in COMPLETE_STATUSES and filled_qty == expected_qty:
            logging.info(
                "Confirmed fill: %s %s qty=%s avg=%.2f order_id=%s",
                transaction_type,
                leg_name,
                filled_qty,
                fill_info["average_price"],
                order_id,
            )
            return fill_info

        if status in COMPLETE_STATUSES and filled_qty != expected_qty:
            raise PartialFillError(f"{leg_name}: unexpected fill quantity {filled_qty}/{expected_qty}", fill_info)

        if status in FAILED_STATUSES:
            raise OrderConfirmationError(f"{leg_name}: {status} - {fill_info['status_message']}")

        time.sleep(config.ORDER_CONFIRM_POLL_SECONDS)

    if last_info and last_info["filled_quantity"] > 0:
        raise PartialFillError(
            f"{leg_name}: timed out with partial fill {last_info['filled_quantity']}/{expected_qty}",
            last_info,
        )
    raise OrderConfirmationError(f"{leg_name}: fill confirmation timed out for order {order_id}")


def _actual_entry_prices(confirmed_fills, fallback_prices):
    prices = dict(fallback_prices)
    for fill in confirmed_fills:
        leg_name = fill["leg_name"]
        avg_price = fill.get("average_price", 0.0)
        if leg_name in prices and avg_price > 0:
            prices[leg_name] = avg_price
    return prices


def _rollback_confirmed_fills(order_api_v3, order_api, confirmed_fills, quantity):
    rollback_failures = []
    for fill in reversed(confirmed_fills):
        reverse_type = "SELL" if fill["transaction_type"] == "BUY" else "BUY"
        rollback_qty = int(fill.get("filled_quantity") or quantity)
        body = _build_market_order(fill["token"], reverse_type, rollback_qty, "iron_fly_rollback")
        try:
            rollback_fill = place_and_confirm(
                order_api_v3,
                order_api,
                body,
                fill["leg_name"],
                fill["token"],
                reverse_type,
            )
            logging.info("Rollback confirmed for %s: %s", fill["leg_name"], rollback_fill["order_id"])
        except Exception as rollback_err:
            logging.critical("Rollback failed for %s: %s", fill["leg_name"], rollback_err)
            rollback_failures.append(fill)
    return rollback_failures


def _defined_loss_rupees(entry_prices, quantity, strikes):
    entry_net = (entry_prices["sell_ce"] + entry_prices["sell_pe"]) - (entry_prices["buy_ce"] + entry_prices["buy_pe"])
    ce_width = abs(strikes.get("buy_ce", 0) - strikes.get("sell_ce", 0))
    pe_width = abs(strikes.get("sell_pe", 0) - strikes.get("buy_pe", 0))
    max_width = max(ce_width, pe_width)
    return max(0.0, (max_width - entry_net) * int(quantity))


def _preflight_risk_check(index_symbol, entry_prices, quantity, strikes):
    validate_trade_quantity(index_symbol, quantity)
    entry_net = (entry_prices["sell_ce"] + entry_prices["sell_pe"]) - (entry_prices["buy_ce"] + entry_prices["buy_pe"])
    if entry_net <= 0:
        raise ValueError(f"Invalid iron butterfly credit: net premium is {entry_net:.2f}")

    defined_loss = _defined_loss_rupees(entry_prices, quantity, strikes)
    if config.MAX_DEFINED_LOSS_RUPEES > 0 and defined_loss > config.MAX_DEFINED_LOSS_RUPEES:
        raise ValueError(
            f"Defined loss {defined_loss:.2f} exceeds MAX_DEFINED_LOSS_RUPEES={config.MAX_DEFINED_LOSS_RUPEES:.2f}"
        )
    return defined_loss


def _normalize_order_sequence(order_sequence):
    normalized = []
    for item in order_sequence or []:
        if len(item) != 2:
            continue
        leg_name, transaction_type = item
        normalized.append((str(leg_name), str(transaction_type).upper()))
    return normalized


def _net_premium_from_order_sequence(prices, order_sequence):
    net = 0.0
    for leg_name, transaction_type in _normalize_order_sequence(order_sequence):
        leg_price = float(prices.get(leg_name, 0.0) or 0.0)
        if transaction_type == "SELL":
            net += leg_price
        elif transaction_type == "BUY":
            net -= leg_price
    return net


def _pnl_from_order_sequence(entry, exits, qty, order_sequence):
    pnl = 0.0
    for leg_name, transaction_type in _normalize_order_sequence(order_sequence):
        entry_price = float(entry.get(leg_name, 0.0) or 0.0)
        exit_price = float(exits.get(leg_name, 0.0) or 0.0)
        if transaction_type == "SELL":
            pnl += (entry_price - exit_price) * qty
        elif transaction_type == "BUY":
            pnl += (exit_price - entry_price) * qty
    return pnl


def _spread_width(strikes):
    values = [float(value) for value in (strikes or {}).values() if value not in (None, "")]
    return max(values) - min(values) if len(values) >= 2 else 0.0


def _preflight_option_spread_check(index_symbol, entry_prices, quantity, strikes, order_sequence):
    validate_trade_quantity(index_symbol, quantity)
    order_sequence = _normalize_order_sequence(order_sequence)
    if not order_sequence:
        raise ValueError("Option spread order sequence is empty.")

    missing = [
        leg_name for leg_name, _ in order_sequence
        if leg_name not in entry_prices or float(entry_prices.get(leg_name, 0.0) or 0.0) <= 0
    ]
    if missing:
        raise ValueError(f"Option spread has missing/zero entry prices: {missing}")

    net_premium = _net_premium_from_order_sequence(entry_prices, order_sequence)
    width = _spread_width(strikes)
    if net_premium >= 0:
        defined_loss = max(0.0, (width - net_premium) * int(quantity))
    else:
        defined_loss = abs(net_premium) * int(quantity)

    if config.MAX_DEFINED_LOSS_RUPEES > 0 and defined_loss > config.MAX_DEFINED_LOSS_RUPEES:
        raise ValueError(
            f"Defined loss {defined_loss:.2f} exceeds MAX_DEFINED_LOSS_RUPEES={config.MAX_DEFINED_LOSS_RUPEES:.2f}"
        )
    return defined_loss, net_premium


def place_option_spread_basket(
    legs,
    index_symbol,
    entry_prices,
    strikes,
    order_sequence,
    strategy_type,
    spot_price=0.0,
    carry_overnight=False,
    metadata=None,
):
    trade_quantity = config.get_nifty_qty() if index_symbol == "NIFTY" else config.get_sensex_qty()
    order_sequence = _normalize_order_sequence(order_sequence)
    try:
        defined_loss, net_premium = _preflight_option_spread_check(
            index_symbol, entry_prices, trade_quantity, strikes, order_sequence
        )
    except ValueError as err:
        logging.critical("Spread preflight risk check failed: %s", err)
        send_telegram_alert(f"<b>BTST ENTRY BLOCKED</b>\n{index_symbol}: {err}")
        return False

    execution_info = {
        "strategy_type": strategy_type,
        "order_sequence": order_sequence,
        "defined_loss_rupees": round(defined_loss, 2),
        "metadata": metadata or {},
    }

    if config.ENVIRONMENT == "SANDBOX":
        logging.info("SANDBOX MODE: simulating %s spread execution.", strategy_type)
        execution_info.update({
            "mode": "SANDBOX",
            "fills": [
                {
                    "leg_name": leg_name,
                    "token": legs[leg_name],
                    "transaction_type": transaction_type,
                    "filled_quantity": int(trade_quantity),
                    "average_price": entry_prices[leg_name],
                    "status": "SIMULATED",
                }
                for leg_name, transaction_type in order_sequence
            ],
        })
        log_trade(
            "ENTRY", index_symbol, entry_prices, net_premium, 0.0,
            f"{strategy_type} Paper Trade (Simulated)", spot_price=spot_price, strikes=strikes
        )
        state_manager.save_state(index_symbol, legs, entry_prices, trade_quantity, strikes, execution_info=execution_info)
        state_manager.update_many({
            "strategy_type": strategy_type,
            "entry_spot": spot_price,
            "carry_overnight": bool(carry_overnight),
            "entry_net_premium": round(net_premium, 4),
        })
        return True

    if config.ENVIRONMENT != "LIVE":
        logging.critical("Unknown ENVIRONMENT=%s. Refusing to trade.", config.ENVIRONMENT)
        return False

    logging.critical("LIVE MODE: routing %s spread orders to Upstox with fill confirmation.", strategy_type)
    order_api_v3, order_api = _make_order_apis()
    confirmed_fills = []
    try:
        for leg_name, transaction_type in order_sequence:
            body = _build_market_order(legs[leg_name], transaction_type, trade_quantity, "btst_entry")
            confirmed_fills.append(
                place_and_confirm(order_api_v3, order_api, body, leg_name, legs[leg_name], transaction_type)
            )
            time.sleep(0.15)
    except ApiException as err:
        logging.error("Live spread order API rejection: %s", getattr(err, "body", err))
    except PartialFillError as err:
        logging.critical("Partial fill during spread entry: %s", err)
        confirmed_fills.append(err.fill_info)
    except Exception as err:
        logging.critical("Spread execution failed before full confirmation: %s", err)

    if len(confirmed_fills) != len(order_sequence):
        logging.critical("Spread entry failed mid-flight. Rolling back only confirmed fills.")
        rollback_failures = _rollback_confirmed_fills(order_api_v3, order_api, confirmed_fills, trade_quantity)
        if rollback_failures:
            actual_prices = _actual_entry_prices(rollback_failures, entry_prices)
            execution_info.update({
                "mode": "LIVE",
                "recovery_required": True,
                "open_after_failed_entry": rollback_failures,
                "confirmed_entry_fills": confirmed_fills,
            })
            state_manager.save_state(index_symbol, legs, actual_prices, trade_quantity, strikes, execution_info=execution_info)
            state_manager.update_many({"strategy_type": strategy_type, "carry_overnight": bool(carry_overnight)})
            send_telegram_alert(
                f"<b>URGENT: BTST ENTRY ROLLBACK FAILED</b>\n{index_symbol}: manual broker reconciliation required."
            )
        return False

    actual_prices = _actual_entry_prices(confirmed_fills, entry_prices)
    actual_net_premium = _net_premium_from_order_sequence(actual_prices, order_sequence)
    execution_info.update({"mode": "LIVE", "fills": confirmed_fills})
    log_trade(
        "ENTRY", index_symbol, actual_prices, actual_net_premium, 0.0,
        f"{strategy_type} Live Basket Confirmed", spot_price=spot_price, strikes=strikes
    )
    state_manager.save_state(index_symbol, legs, actual_prices, trade_quantity, strikes, execution_info=execution_info)
    state_manager.update_many({
        "strategy_type": strategy_type,
        "entry_spot": spot_price,
        "carry_overnight": bool(carry_overnight),
        "entry_net_premium": round(actual_net_premium, 4),
    })
    send_telegram_alert(
        f"<b>BTST TRADE DEPLOYED: {index_symbol}</b>\n"
        f"Strategy: {strategy_type}\n"
        f"Net Premium: {actual_net_premium:.2f}\n"
        f"Quantity: {trade_quantity}\n"
        f"Max Defined Loss: {defined_loss:.2f}"
    )
    return True


def place_iron_butterfly_basket(legs, index_symbol, entry_prices, strikes, spot_price=0.0):
    trade_quantity = config.get_nifty_qty() if index_symbol == "NIFTY" else config.get_sensex_qty()
    try:
        defined_loss = _preflight_risk_check(index_symbol, entry_prices, trade_quantity, strikes)
    except ValueError as err:
        logging.critical("Preflight risk check failed: %s", err)
        send_telegram_alert(f"<b>ENTRY BLOCKED</b>\n{index_symbol}: {err}")
        return False

    if config.ENVIRONMENT == "SANDBOX":
        logging.info("SANDBOX MODE: simulating local paper execution.")
        net_premium = (entry_prices["sell_ce"] + entry_prices["sell_pe"]) - (entry_prices["buy_ce"] + entry_prices["buy_pe"])
        execution_info = {
            "mode": "SANDBOX",
            "defined_loss_rupees": round(defined_loss, 2),
            "fills": [
                {"leg_name": name, "token": token, "transaction_type": "SELL" if name.startswith("sell") else "BUY",
                 "filled_quantity": int(trade_quantity), "average_price": entry_prices[name], "status": "SIMULATED"}
                for name, token in legs.items()
            ],
        }
        logging.info("Simulated basket executed successfully.")
        log_trade(
            "ENTRY", index_symbol, entry_prices, net_premium, 0.0,
            "Local Paper Trade (Simulated)", spot_price=spot_price, strikes=strikes
        )
        state_manager.save_state(index_symbol, legs, entry_prices, trade_quantity, strikes, execution_info=execution_info)
        state_manager.update_state("entry_spot", spot_price)
        return True

    if config.ENVIRONMENT != "LIVE":
        logging.critical("Unknown ENVIRONMENT=%s. Refusing to trade.", config.ENVIRONMENT)
        return False

    logging.critical("LIVE MODE: routing orders to Upstox with fill confirmation.")
    order_api_v3, order_api = _make_order_apis()
    orders = [
        ("buy_ce", legs["buy_ce"], "BUY"),
        ("buy_pe", legs["buy_pe"], "BUY"),
        ("sell_ce", legs["sell_ce"], "SELL"),
        ("sell_pe", legs["sell_pe"], "SELL"),
    ]

    confirmed_fills = []
    try:
        for leg_name, token, tx_type in orders:
            body = _build_market_order(token, tx_type, trade_quantity, "iron_fly_entry")
            confirmed_fills.append(place_and_confirm(order_api_v3, order_api, body, leg_name, token, tx_type))
            time.sleep(0.15)
    except ApiException as err:
        logging.error("Live order API rejection: %s", getattr(err, "body", err))
    except PartialFillError as err:
        logging.critical("Partial fill during entry: %s", err)
        confirmed_fills.append(err.fill_info)
    except Exception as err:
        logging.critical("Basket execution failed before full confirmation: %s", err)

    if len(confirmed_fills) != len(orders):
        logging.critical("Entry failed mid-flight. Rolling back only confirmed fills.")
        rollback_failures = _rollback_confirmed_fills(order_api_v3, order_api, confirmed_fills, trade_quantity)
        if rollback_failures:
            actual_prices = _actual_entry_prices(rollback_failures, entry_prices)
            execution_info = {
                "mode": "LIVE",
                "recovery_required": True,
                "defined_loss_rupees": round(defined_loss, 2),
                "open_after_failed_entry": rollback_failures,
                "confirmed_entry_fills": confirmed_fills,
            }
            state_manager.save_state(index_symbol, legs, actual_prices, trade_quantity, strikes, execution_info=execution_info)
            send_telegram_alert(
                f"<b>URGENT: ENTRY ROLLBACK FAILED</b>\n{index_symbol}: manual broker reconciliation required."
            )
        return False

    actual_prices = _actual_entry_prices(confirmed_fills, entry_prices)
    net_premium = (actual_prices["sell_ce"] + actual_prices["sell_pe"]) - (actual_prices["buy_ce"] + actual_prices["buy_pe"])
    execution_info = {
        "mode": "LIVE",
        "defined_loss_rupees": round(defined_loss, 2),
        "fills": confirmed_fills,
    }
    log_trade(
        "ENTRY", index_symbol, actual_prices, net_premium, 0.0,
        "Live Basket Confirmed", spot_price=spot_price, strikes=strikes
    )
    state_manager.save_state(index_symbol, legs, actual_prices, trade_quantity, strikes, execution_info=execution_info)
    state_manager.update_state("entry_spot", spot_price)
    send_telegram_alert(
        f"<b>TRADE DEPLOYED: {index_symbol}</b>\n"
        f"Net Premium Collected: {net_premium:.2f}\n"
        f"Quantity: {trade_quantity}\n"
        f"Max Defined Loss: {defined_loss:.2f}"
    )
    return True


def _fresh_exit_prices_from_broker(legs):
    try:
        quotes = get_broker().get_fresh_option_quotes(list(legs.values()))
    except Exception as err:
        logging.warning("Fresh exit quote fetch failed: %s", err)
        return None

    prices = {
        leg_name: quotes.get(token, 0)
        for leg_name, token in legs.items()
    }
    return prices if all(value > 0 for value in prices.values()) else None


def _safe_exit_prices(exit_prices, entry, legs=None, prefer_fresh=False):
    exit_prices = exit_prices or {}
    leg_names = tuple(entry.keys())
    if prefer_fresh and legs:
        fresh_prices = _fresh_exit_prices_from_broker(legs)
        if fresh_prices:
            return fresh_prices, "Fresh broker quote snapshot"

    all_zeros = exit_prices and all(exit_prices.get(k, 0) == 0 for k in leg_names)
    has_missing = any(exit_prices.get(k, 0) <= 0 for k in leg_names)
    if (all_zeros or has_missing) and legs:
        fresh_prices = _fresh_exit_prices_from_broker(legs)
        if fresh_prices:
            return fresh_prices, "Fresh broker quote snapshot"

    if all_zeros or has_missing:
        logging.warning("Exit prices missing/zero. Falling back to entry prices for accounting only.")
        note = "Estimated from entry prices; fresh quote unavailable"
    else:
        note = ""
    return ({
        key: exit_prices.get(key, 0) if exit_prices.get(key, 0) > 0 else entry[key]
        for key in leg_names
    }, note)


def _pnl(entry, exits, qty):
    pnl = (entry["sell_ce"] + entry["sell_pe"] - exits["sell_ce"] - exits["sell_pe"]) * qty
    pnl += (exits["buy_ce"] + exits["buy_pe"] - entry["buy_ce"] - entry["buy_pe"]) * qty
    return pnl


def _actual_exit_prices(exit_fills, fallback_prices):
    prices = dict(fallback_prices)
    for fill in exit_fills:
        leg_name = fill.get("leg_name")
        avg_price = float(fill.get("average_price", 0.0) or 0.0)
        if leg_name in prices and avg_price > 0:
            prices[leg_name] = avg_price
    return prices


def _state_order_sequence(state):
    execution_info = state.get("execution_info", {}) if state else {}
    return _normalize_order_sequence(execution_info.get("order_sequence", []))


def _square_off_option_spread(state, exit_prices=None, exit_reason=""):
    success = True
    entry = state["entry_prices"]
    qty = int(state.get("quantity", 0))
    legs = state["legs"]
    index_symbol = state["index_symbol"]
    order_sequence = _state_order_sequence(state)
    strategy_type = state.get("strategy_type") or state.get("execution_info", {}).get("strategy_type", "OPTION_SPREAD")
    prefer_fresh = exit_reason in ("MANUAL_EXIT", "GRACEFUL_STOP", "SOCKET_DEAD_EXIT")
    safe_exits, price_note = _safe_exit_prices(exit_prices, entry, legs, prefer_fresh=prefer_fresh)
    strikes = state.get("strikes", {})
    spot_price = state.get("last_spot") or state.get("entry_spot") or 0
    notes = f"{strategy_type} Paper Trade Closed" if config.ENVIRONMENT == "SANDBOX" else f"{strategy_type} Live Exit"
    if price_note:
        notes = f"{notes} ({price_note})"

    if config.ENVIRONMENT == "SANDBOX":
        exit_premium = _net_premium_from_order_sequence(safe_exits, order_sequence)
        pnl = _pnl_from_order_sequence(entry, safe_exits, qty, order_sequence)
        log_trade(
            "EXIT", index_symbol, safe_exits, exit_premium, pnl, notes,
            spot_price=spot_price, strikes=strikes, exit_reason=exit_reason
        )
        logging.info("Simulated PnL for %s: %.2f", strategy_type, pnl)
        send_telegram_alert(
            f"<b>BTST TRADE CLOSED (PAPER): {index_symbol}</b>\n"
            f"Strategy: {strategy_type}\n"
            f"Reason: <b>{exit_reason or 'EXIT'}</b>\n"
            f"Realized PnL: <b>{pnl:.2f}</b>\n"
            f"Net Premium Exited: {exit_premium:.2f}"
        )
        state_manager.clear_state()
        return

    if config.ENVIRONMENT != "LIVE":
        logging.critical("Unknown ENVIRONMENT=%s. State retained.", config.ENVIRONMENT)
        return

    logging.critical("Routing %s spread exit orders to LIVE Upstox with fill confirmation.", strategy_type)
    order_api_v3, order_api = _make_order_apis()
    exit_fills = []

    def close_leg(leg_name, tx_type):
        body = _build_market_order(legs[leg_name], tx_type, qty, "btst_exit")
        fill = place_and_confirm(order_api_v3, order_api, body, leg_name, legs[leg_name], tx_type)
        exit_fills.append(fill)
        time.sleep(0.15)
        return fill

    short_legs = [(leg_name, tx_type) for leg_name, tx_type in order_sequence if tx_type == "SELL"]
    long_legs = [(leg_name, tx_type) for leg_name, tx_type in order_sequence if tx_type == "BUY"]
    shorts_closed = True

    for leg_name, _ in short_legs:
        try:
            close_leg(leg_name, "BUY")
        except Exception as err:
            success = False
            shorts_closed = False
            logging.critical("Failed to close short spread leg %s. Error: %s", leg_name, err)

    if shorts_closed:
        for leg_name, _ in long_legs:
            try:
                close_leg(leg_name, "SELL")
            except Exception as err:
                success = False
                logging.critical("Failed to close long spread leg %s. Error: %s", leg_name, err)
    else:
        logging.critical("Keeping long spread hedges intact because one or more short legs failed to close.")

    safe_exits = _actual_exit_prices(exit_fills, safe_exits)
    exit_premium = _net_premium_from_order_sequence(safe_exits, order_sequence)
    pnl = _pnl_from_order_sequence(entry, safe_exits, qty, order_sequence)
    if exit_fills:
        notes = f"{notes} (Broker fill averages applied)"

    log_trade(
        "EXIT", index_symbol, safe_exits, exit_premium, pnl, notes,
        spot_price=spot_price, strikes=strikes, exit_reason=exit_reason
    )
    state_manager.update_many({"last_exit_fills": exit_fills, "last_exit_success": success})

    send_telegram_alert(
        f"<b>BTST TRADE CLOSED (LIVE): {index_symbol}</b>\n"
        f"Strategy: {strategy_type}\n"
        f"Reason: <b>{exit_reason or 'EXIT'}</b>\n"
        f"Realized PnL: <b>{pnl:.2f}</b>\n"
        f"Net Premium Exited: {exit_premium:.2f}\n"
        f"Status: {'Execution Safe' if success else 'WARNING: Leg Failure'}"
    )

    if success:
        state_manager.clear_state()
    else:
        logging.critical("STATE RETAINED: spread exit execution failed. Manual recovery required.")


def square_off_all(exit_prices=None, exit_reason=""):
    logging.critical("TRIGGERING SQUARE OFF SEQUENCE!")
    success = True
    state = state_manager.load_state()

    if not state:
        log_trade("EXIT", "UNKNOWN", {}, 0.0, 0.0, "Emergency Square Off", exit_reason=exit_reason)
        send_telegram_alert("<b>EMERGENCY SQUARE OFF TRIGGERED!</b> Check terminal immediately.")
        return

    if _state_order_sequence(state):
        _square_off_option_spread(state, exit_prices=exit_prices, exit_reason=exit_reason)
        return

    entry = state["entry_prices"]
    qty = int(state.get("quantity", 0))
    legs = state["legs"]
    index_symbol = state["index_symbol"]
    prefer_fresh = exit_reason in ("MANUAL_EXIT", "GRACEFUL_STOP", "SOCKET_DEAD_EXIT")
    safe_exits, price_note = _safe_exit_prices(exit_prices, entry, legs, prefer_fresh=prefer_fresh)
    strikes = state.get("strikes", {})
    spot_price = state.get("last_spot") or state.get("entry_spot") or strikes.get("sell_ce", 0)
    notes = "Local Paper Trade Closed" if config.ENVIRONMENT == "SANDBOX" else "Live Exchange Exit"
    if price_note:
        notes = f"{notes} ({price_note})"

    if config.ENVIRONMENT == "SANDBOX":
        exit_premium = (safe_exits["sell_ce"] + safe_exits["sell_pe"]) - (safe_exits["buy_ce"] + safe_exits["buy_pe"])
        pnl = _pnl(entry, safe_exits, qty)
        log_trade(
            "EXIT", index_symbol, safe_exits, exit_premium, pnl, notes,
            spot_price=spot_price, strikes=strikes, exit_reason=exit_reason
        )
        logging.info("Simulated PnL for this trade: %.2f", pnl)
        send_telegram_alert(
            f"<b>TRADE CLOSED (PAPER): {index_symbol}</b>\n"
            f"Reason: <b>{exit_reason or 'EXIT'}</b>\n"
            f"Realized PnL: <b>{pnl:.2f}</b>\n"
            f"Net Premium Exited: {exit_premium:.2f}"
        )
        state_manager.clear_state()
        return

    if config.ENVIRONMENT != "LIVE":
        logging.critical("Unknown ENVIRONMENT=%s. State retained.", config.ENVIRONMENT)
        return

    logging.critical("Routing exit orders to LIVE Upstox with fill confirmation.")
    order_api_v3, order_api = _make_order_apis()
    exit_fills = []

    def close_leg(leg_name, tx_type):
        body = _build_market_order(legs[leg_name], tx_type, qty, "iron_fly_exit")
        fill = place_and_confirm(order_api_v3, order_api, body, leg_name, legs[leg_name], tx_type)
        exit_fills.append(fill)
        time.sleep(0.15)
        return fill

    for short_leg, hedge_leg in (("sell_ce", "buy_ce"), ("sell_pe", "buy_pe")):
        try:
            close_leg(short_leg, "BUY")
        except Exception as err:
            success = False
            logging.critical("Failed to close %s. Keeping %s hedge intact. Error: %s", short_leg, hedge_leg, err)
            continue

        try:
            close_leg(hedge_leg, "SELL")
        except Exception as err:
            success = False
            logging.critical("Short %s closed, but hedge %s close failed: %s", short_leg, hedge_leg, err)

    safe_exits = _actual_exit_prices(exit_fills, safe_exits)
    exit_premium = (safe_exits["sell_ce"] + safe_exits["sell_pe"]) - (safe_exits["buy_ce"] + safe_exits["buy_pe"])
    pnl = _pnl(entry, safe_exits, qty)
    if exit_fills:
        notes = f"{notes} (Broker fill averages applied)"

    log_trade(
        "EXIT", index_symbol, safe_exits, exit_premium, pnl, notes,
        spot_price=spot_price, strikes=strikes, exit_reason=exit_reason
    )
    state_manager.update_many({"last_exit_fills": exit_fills, "last_exit_success": success})

    send_telegram_alert(
        f"<b>TRADE CLOSED (LIVE): {index_symbol}</b>\n"
        f"Reason: <b>{exit_reason or 'EXIT'}</b>\n"
        f"Realized PnL: <b>{pnl:.2f}</b>\n"
        f"Net Premium Exited: {exit_premium:.2f}\n"
        f"Status: {'Execution Safe' if success else 'WARNING: Leg Failure'}"
    )

    if success:
        state_manager.clear_state()
    else:
        logging.critical("STATE RETAINED: exit execution failed. Manual recovery required.")
