# logger.py
import csv
import os
from datetime import datetime

import config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "sandbox_trade_logs.csv")
FIELDNAMES = [
    "Timestamp",
    "Action",
    "Index",
    "Index_Name",
    "Strategy_Type",
    "Broker_Lot_Size",
    "Total_Lots_Deployed",
    "Total_Quantity",
    "Margin_Blocked",
    "Spot_Price",
    "Sell_CE_Strike",
    "Sell_PE_Strike",
    "Sell_CE",
    "Sell_PE",
    "Buy_CE",
    "Buy_PE",
    "Net_Premium",
    "PnL",
    "Exit_Reason",
    "Notes",
]


def _lot_size_for_index(index_symbol):
    if index_symbol == "NIFTY":
        return config.NIFTY_LOT_MULTIPLE
    if index_symbol == "SENSEX":
        return config.SENSEX_LOT_MULTIPLE
    return ""


def _normalize_existing_file():
    if not os.path.exists(LOG_FILE):
        return
    with open(LOG_FILE, newline="") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames == FIELDNAMES:
            return
        rows = list(reader)

    with open(LOG_FILE, mode="w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows:
            normalized = {field: row.get(field, "") for field in FIELDNAMES}
            normalized["Index_Name"] = normalized.get("Index_Name") or row.get("Index", "")
            normalized["Broker_Lot_Size"] = normalized.get("Broker_Lot_Size") or _lot_size_for_index(row.get("Index", ""))
            writer.writerow(normalized)


def init_logger():
    _normalize_existing_file()
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, mode='w', newline='') as file:
            writer = csv.DictWriter(file, fieldnames=FIELDNAMES)
            writer.writeheader()


def log_trade(
    action,
    index_symbol,
    prices,
    net_premium,
    pnl,
    notes,
    spot_price=0.0,
    strikes=None,
    exit_reason="",
    strategy_type="",
    broker_lot_size=None,
    total_lots_deployed=None,
    total_quantity=None,
    margin_blocked=None,
):
    init_logger()
    with open(LOG_FILE, mode='a', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=FIELDNAMES)
        strikes = strikes or {}
        broker_lot_size = broker_lot_size if broker_lot_size is not None else _lot_size_for_index(index_symbol)
        if total_quantity is not None and total_lots_deployed is None and broker_lot_size:
            total_lots_deployed = int(total_quantity) // int(broker_lot_size)
        writer.writerow({
            "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "Action": action,
            "Index": index_symbol,
            "Index_Name": index_symbol,
            "Strategy_Type": strategy_type,
            "Broker_Lot_Size": broker_lot_size if broker_lot_size is not None else "",
            "Total_Lots_Deployed": total_lots_deployed if total_lots_deployed is not None else "",
            "Total_Quantity": total_quantity if total_quantity is not None else "",
            "Margin_Blocked": round(float(margin_blocked or 0.0), 2) if margin_blocked not in (None, "") else "",
            "Spot_Price": round(float(spot_price or 0), 2),
            "Sell_CE_Strike": strikes.get("sell_ce", ""),
            "Sell_PE_Strike": strikes.get("sell_pe", ""),
            "Sell_CE": round(prices.get('sell_ce', 0), 2),
            "Sell_PE": round(prices.get('sell_pe', 0), 2),
            "Buy_CE": round(prices.get('buy_ce', 0), 2),
            "Buy_PE": round(prices.get('buy_pe', 0), 2),
            "Net_Premium": round(net_premium, 2),
            "PnL": round(pnl, 2),
            "Exit_Reason": exit_reason,
            "Notes": notes,
        })
