# logger.py
import csv
import os
from datetime import datetime

#LOG_FILE = "sandbox_trade_logs.csv"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "sandbox_trade_logs.csv")

def init_logger():
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, mode='w', newline='') as file:
            writer = csv.writer(file)
            writer.writerow(["Timestamp", "Action", "Index", "Sell_CE", "Sell_PE", "Buy_CE", "Buy_PE", "Net_Premium", "PnL", "Notes"])

def log_trade(action, index_symbol, prices, net_premium, pnl, notes):
    init_logger()
    with open(LOG_FILE, mode='a', newline='') as file:
        writer = csv.writer(file)
        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            action,
            index_symbol,
            round(prices.get('sell_ce', 0), 2),
            round(prices.get('sell_pe', 0), 2),
            round(prices.get('buy_ce', 0), 2),
            round(prices.get('buy_pe', 0), 2),
            round(net_premium, 2),
            round(pnl, 2),
            notes
        ])
