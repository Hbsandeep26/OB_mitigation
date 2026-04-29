# logger.py
import csv
import os
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "sandbox_trade_logs.csv")
FIELDNAMES = [
    "Timestamp",
    "Action",
    "Index",
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
            writer.writerow(normalized)


def init_logger():
    _normalize_existing_file()
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, mode='w', newline='') as file:
            writer = csv.DictWriter(file, fieldnames=FIELDNAMES)
            writer.writeheader()


def log_trade(action, index_symbol, prices, net_premium, pnl, notes, spot_price=0.0, strikes=None, exit_reason=""):
    init_logger()
    with open(LOG_FILE, mode='a', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=FIELDNAMES)
        strikes = strikes or {}
        writer.writerow({
            "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "Action": action,
            "Index": index_symbol,
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
