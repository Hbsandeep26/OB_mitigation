import logging

import config
from broker import get_broker


def _is_directional_strategy(strategy_type):
    strategy_type = str(strategy_type or "").upper()
    return (
        strategy_type.startswith("BTST_")
        or "CREDIT_SPREAD" in strategy_type
        or "DEBIT_SPREAD" in strategy_type
    )


def static_margin_per_lot(strategy_type, current_vix):
    strategy_type = str(strategy_type or "").upper()
    vix = float(current_vix or 0.0)

    if strategy_type == "IRON_BUTTERFLY":
        return (
            config.IRON_BUTTERFLY_MARGIN_LOW_VIX
            if vix < config.IRON_BUTTERFLY_VIX_ACTIVATION
            else config.IRON_BUTTERFLY_MARGIN_HIGH_VIX
        )
    if strategy_type == "IRON_CONDOR":
        return (
            config.IRON_CONDOR_MARGIN_HIGH_VIX
            if vix >= config.IRON_CONDOR_VIX_ACTIVATION
            else config.IRON_CONDOR_MARGIN_LOW_VIX
        )
    if _is_directional_strategy(strategy_type):
        return config.DIRECTIONAL_SPREAD_MARGIN
    return config.DIRECTIONAL_SPREAD_MARGIN


def _route_order_requests(route, lot_multiple):
    legs = getattr(route, "legs", None) or {}
    order_sequence = getattr(route, "order_sequence", None) or []
    if not order_sequence:
        order_sequence = [
            (leg_name, "SELL" if str(leg_name).startswith("sell") else "BUY")
            for leg_name in legs
        ]

    requests = []
    for leg_name, transaction_type in order_sequence:
        token = legs.get(leg_name)
        if not token:
            continue
        requests.append({
            "instrument_key": token,
            "quantity": int(lot_multiple),
            "transaction_type": str(transaction_type).upper(),
            "product": "I",
        })
    return requests


def _basket_margin_per_lot(route, lot_multiple):
    if not route:
        return None
    requests = _route_order_requests(route, lot_multiple)
    if not requests:
        return None
    try:
        margin = get_broker().get_order_margin(requests)
    except Exception as err:
        logging.warning("Live basket margin unavailable; using static margin fallback: %s", err)
        return None
    try:
        margin = float(margin or 0.0)
    except (TypeError, ValueError):
        return None
    return margin if margin > 0 else None


def _available_margin(environment, ui_virtual_capital):
    if str(environment).upper() == "SANDBOX":
        return float(ui_virtual_capital or 0.0), {
            "source": "SANDBOX_VIRTUAL_CAPITAL",
            "requires_live_funds": False,
        }

    if str(environment).upper() != "LIVE":
        return 0.0, {"source": "UNKNOWN_ENVIRONMENT", "requires_live_funds": True}

    try:
        return get_broker().get_available_margin()
    except Exception as err:
        logging.critical("Live funds unavailable; rejecting entry: %s", err)
        return 0.0, {
            "source": "LIVE_FUNDS_UNAVAILABLE",
            "requires_live_funds": True,
            "error": str(err),
        }


def calculate_position_size(environment, strategy_type, current_vix, ui_virtual_capital, index_symbol, lot_multiple, route=None):
    environment = str(environment or "").upper()
    available_margin, source_metadata = _available_margin(environment, ui_virtual_capital)
    usable_capital = available_margin * config.MAX_CAPITAL_UTILIZATION

    margin_source = "STATIC"
    margin_per_lot = None
    if environment == "LIVE":
        margin_per_lot = _basket_margin_per_lot(route, lot_multiple)
        if margin_per_lot:
            margin_source = "UPSTOX_BASKET_MARGIN"

    if not margin_per_lot:
        margin_per_lot = static_margin_per_lot(strategy_type, current_vix)

    result = {
        "status": "REJECTED",
        "environment": environment,
        "strategy_type": strategy_type,
        "index_symbol": index_symbol,
        "lot_multiple": int(lot_multiple),
        "available_margin": round(float(available_margin or 0.0), 2),
        "usable_capital": round(float(usable_capital or 0.0), 2),
        "max_capital_utilization": config.MAX_CAPITAL_UTILIZATION,
        "margin_per_lot": round(float(margin_per_lot or 0.0), 2),
        "margin_source": margin_source,
        "lots_to_deploy": 0,
        "quantity": 0,
        "capital_deployed": 0.0,
        "metadata": source_metadata,
    }

    if environment == "LIVE" and source_metadata.get("source") == "LIVE_FUNDS_UNAVAILABLE":
        result["reason"] = "Live funds unavailable"
        return result

    if usable_capital <= 0 or margin_per_lot <= 0:
        result["reason"] = "No usable capital"
        return result

    max_lots = int(usable_capital // margin_per_lot)
    if max_lots < 1:
        result["reason"] = "Insufficient margin"
        return result

    result.update({
        "status": "APPROVED",
        "reason": "",
        "lots_to_deploy": max_lots,
        "quantity": max_lots * int(lot_multiple),
        "capital_deployed": round(max_lots * float(margin_per_lot), 2),
    })
    return result
