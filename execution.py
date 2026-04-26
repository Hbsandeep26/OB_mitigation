import logging
import time

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
    configuration = upstox_client.Configuration()
    configuration.access_token = config.get_live_token()
    api_client = upstox_client.ApiClient(configuration)
    return upstox_client.OrderApiV3(api_client), upstox_client.OrderApi(api_client)


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


def place_iron_butterfly_basket(legs, index_symbol, entry_prices, strikes):
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
        log_trade("ENTRY", index_symbol, entry_prices, net_premium, 0.0, "Local Paper Trade (Simulated)")
        state_manager.save_state(index_symbol, legs, entry_prices, trade_quantity, strikes, execution_info=execution_info)
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
    log_trade("ENTRY", index_symbol, actual_prices, net_premium, 0.0, "Live Basket Confirmed")
    state_manager.save_state(index_symbol, legs, actual_prices, trade_quantity, strikes, execution_info=execution_info)
    send_telegram_alert(
        f"<b>TRADE DEPLOYED: {index_symbol}</b>\n"
        f"Net Premium Collected: {net_premium:.2f}\n"
        f"Quantity: {trade_quantity}\n"
        f"Max Defined Loss: {defined_loss:.2f}"
    )
    return True


def _safe_exit_prices(exit_prices, entry):
    exit_prices = exit_prices or {}
    all_zeros = exit_prices and all(exit_prices.get(k, 0) == 0 for k in ("sell_ce", "sell_pe", "buy_ce", "buy_pe"))
    if all_zeros:
        logging.warning("All exit prices are zero. Falling back to entry prices for accounting only.")
    return {
        "sell_ce": exit_prices.get("sell_ce", entry["sell_ce"]) if not all_zeros else entry["sell_ce"],
        "sell_pe": exit_prices.get("sell_pe", entry["sell_pe"]) if not all_zeros else entry["sell_pe"],
        "buy_ce": exit_prices.get("buy_ce", entry["buy_ce"]) if not all_zeros else entry["buy_ce"],
        "buy_pe": exit_prices.get("buy_pe", entry["buy_pe"]) if not all_zeros else entry["buy_pe"],
    }


def _pnl(entry, exits, qty):
    pnl = (entry["sell_ce"] + entry["sell_pe"] - exits["sell_ce"] - exits["sell_pe"]) * qty
    pnl += (exits["buy_ce"] + exits["buy_pe"] - entry["buy_ce"] - entry["buy_pe"]) * qty
    return pnl


def square_off_all(exit_prices=None):
    logging.critical("TRIGGERING SQUARE OFF SEQUENCE!")
    success = True
    state = state_manager.load_state()

    if not state:
        log_trade("EXIT", "UNKNOWN", {}, 0.0, 0.0, "Emergency Square Off")
        send_telegram_alert("<b>EMERGENCY SQUARE OFF TRIGGERED!</b> Check terminal immediately.")
        return

    entry = state["entry_prices"]
    qty = int(state.get("quantity", 0))
    legs = state["legs"]
    index_symbol = state["index_symbol"]
    safe_exits = _safe_exit_prices(exit_prices, entry)
    exit_premium = (safe_exits["sell_ce"] + safe_exits["sell_pe"]) - (safe_exits["buy_ce"] + safe_exits["buy_pe"])
    pnl = _pnl(entry, safe_exits, qty)

    if config.ENVIRONMENT == "SANDBOX":
        log_trade("EXIT", index_symbol, safe_exits, exit_premium, pnl, "Local Paper Trade Closed")
        logging.info("Simulated PnL for this trade: %.2f", pnl)
        send_telegram_alert(f"<b>TRADE CLOSED (PAPER): {index_symbol}</b>\nRealized PnL: <b>{pnl:.2f}</b>")
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

    log_trade("EXIT", index_symbol, safe_exits, exit_premium, pnl, "Live Exchange Exit")
    state_manager.update_many({"last_exit_fills": exit_fills, "last_exit_success": success})

    send_telegram_alert(
        f"<b>TRADE CLOSED (LIVE): {index_symbol}</b>\n"
        f"Realized PnL: <b>{pnl:.2f}</b>\n"
        f"Net Premium Exited: {exit_premium:.2f}\n"
        f"Status: {'Execution Safe' if success else 'WARNING: Leg Failure'}"
    )

    if success:
        state_manager.clear_state()
    else:
        logging.critical("STATE RETAINED: exit execution failed. Manual recovery required.")
