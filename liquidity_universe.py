"""Curated liquid instruments for research and batch backtests.

Security ids are the exchange security ids used by Dhan for NSE/BSE market
data. Keep this list deliberately boring: broad indices first, then highly
traded large-cap cash names that usually have tight spreads and active futures.
"""

from __future__ import annotations

import json
import math
from pathlib import Path


LIQUIDITY_UNIVERSE = [
    {
        "symbol": "NIFTY",
        "name": "Nifty 50",
        "asset_class": "INDEX",
        "exchange_segment": "IDX_I",
        "instrument": "INDEX",
        "security_id": "13",
        "lot_size": 65,
        "aliases": ["NIFTY50"],
    },
    {
        "symbol": "SENSEX",
        "name": "BSE Sensex",
        "asset_class": "INDEX",
        "exchange_segment": "IDX_I",
        "instrument": "INDEX",
        "security_id": "51",
        "lot_size": 20,
        "aliases": ["SENNEX"],
    },
    {"symbol": "BANKNIFTY", "name": "Nifty Bank", "asset_class": "INDEX", "exchange_segment": "IDX_I", "instrument": "INDEX", "security_id": "25", "lot_size": 30, "aliases": ["NIFTYBANK"]},
    {"symbol": "RELIANCE", "name": "Reliance Industries", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "2885", "lot_size": 1},
    {"symbol": "HDFCBANK", "name": "HDFC Bank", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "1333", "lot_size": 1},
    {"symbol": "ICICIBANK", "name": "ICICI Bank", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "4963", "lot_size": 1},
    {"symbol": "INFY", "name": "Infosys", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "1594", "lot_size": 1},
    {"symbol": "TCS", "name": "Tata Consultancy Services", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "11536", "lot_size": 1},
    {"symbol": "SBIN", "name": "State Bank of India", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "3045", "lot_size": 1},
    {"symbol": "AXISBANK", "name": "Axis Bank", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "5900", "lot_size": 1},
    {"symbol": "KOTAKBANK", "name": "Kotak Mahindra Bank", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "1922", "lot_size": 1},
    {"symbol": "LT", "name": "Larsen and Toubro", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "11483", "lot_size": 1},
    {"symbol": "ITC", "name": "ITC", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "1660", "lot_size": 1},
    {"symbol": "BHARTIARTL", "name": "Bharti Airtel", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "10604", "lot_size": 1},
    {"symbol": "HINDUNILVR", "name": "Hindustan Unilever", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "1394", "lot_size": 1},
    {"symbol": "BAJFINANCE", "name": "Bajaj Finance", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "317", "lot_size": 1},
    {"symbol": "HCLTECH", "name": "HCL Technologies", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "7229", "lot_size": 1},
    {"symbol": "MARUTI", "name": "Maruti Suzuki", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "10999", "lot_size": 1},
    {"symbol": "M&M", "name": "Mahindra and Mahindra", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "2031", "lot_size": 1, "aliases": ["MNM", "M&M"]},
    {"symbol": "TATAMOTORS", "name": "Tata Motors", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "3456", "lot_size": 1},
    {"symbol": "TATASTEEL", "name": "Tata Steel", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "3499", "lot_size": 1},
    {"symbol": "SUNPHARMA", "name": "Sun Pharmaceutical", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "3351", "lot_size": 1},
    {"symbol": "ULTRACEMCO", "name": "UltraTech Cement", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "11532", "lot_size": 1},
    {"symbol": "NTPC", "name": "NTPC", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "11630", "lot_size": 1},
    {"symbol": "POWERGRID", "name": "Power Grid Corporation", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "14977", "lot_size": 1},
    {"symbol": "ADANIENT", "name": "Adani Enterprises", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "25", "lot_size": 1},
    {"symbol": "ADANIPORTS", "name": "Adani Ports", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "15083", "lot_size": 1},
    {"symbol": "WIPRO", "name": "Wipro", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "3787", "lot_size": 1},
    {"symbol": "TECHM", "name": "Tech Mahindra", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "13538", "lot_size": 1},
    {"symbol": "ASIANPAINT", "name": "Asian Paints", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "236", "lot_size": 1},
    {"symbol": "TITAN", "name": "Titan Company", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "3506", "lot_size": 1},
    {"symbol": "BAJAJFINSV", "name": "Bajaj Finserv", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "16675", "lot_size": 1},
    {"symbol": "HDFCLIFE", "name": "HDFC Life Insurance", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "467", "lot_size": 1},
    {"symbol": "SBILIFE", "name": "SBI Life Insurance", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "21808", "lot_size": 1},
    {"symbol": "COALINDIA", "name": "Coal India", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "20374", "lot_size": 1},
    {"symbol": "ONGC", "name": "Oil and Natural Gas Corporation", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "2475", "lot_size": 1},
    {"symbol": "IOC", "name": "Indian Oil Corporation", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "1624", "lot_size": 1},
    {"symbol": "HINDALCO", "name": "Hindalco Industries", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "1363", "lot_size": 1},
    {"symbol": "JSWSTEEL", "name": "JSW Steel", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "11723", "lot_size": 1},
    {"symbol": "CIPLA", "name": "Cipla", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "694", "lot_size": 1},
    {"symbol": "DRREDDY", "name": "Dr. Reddy's Laboratories", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "881", "lot_size": 1},
    {"symbol": "APOLLOHOSP", "name": "Apollo Hospitals", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "157", "lot_size": 1},
    {"symbol": "EICHERMOT", "name": "Eicher Motors", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "910", "lot_size": 1},
    {"symbol": "HEROMOTOCO", "name": "Hero MotoCorp", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "1348", "lot_size": 1},
    {"symbol": "TATACONSUM", "name": "Tata Consumer Products", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "3432", "lot_size": 1},
    {"symbol": "BRITANNIA", "name": "Britannia Industries", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "547", "lot_size": 1},
    {"symbol": "NESTLEIND", "name": "Nestle India", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "17963", "lot_size": 1},
    {"symbol": "GRASIM", "name": "Grasim Industries", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "1232", "lot_size": 1},
    {"symbol": "BAJAJ-AUTO", "name": "Bajaj Auto", "asset_class": "EQUITY", "exchange_segment": "NSE_EQ", "instrument": "EQUITY", "security_id": "16669", "lot_size": 1},
]


def _norm(value: str) -> str:
    return str(value or "").strip().upper()


def load_universe(universe_file: str | None = None) -> list[dict]:
    if not universe_file:
        return [dict(item) for item in LIQUIDITY_UNIVERSE]

    path = Path(universe_file)
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("Universe file must contain a JSON array of instrument objects")
    return data


def select_universe(symbols: str | None = None, universe_file: str | None = None) -> list[dict]:
    instruments = load_universe(universe_file)
    if not symbols:
        return instruments

    requested = {_norm(item) for item in symbols.split(",") if _norm(item)}
    selected = []
    for instrument in instruments:
        aliases = {_norm(instrument.get("symbol")), *{_norm(a) for a in instrument.get("aliases", [])}}
        if aliases & requested:
            selected.append(instrument)
    missing = requested - {_norm(item.get("symbol")) for item in selected} - {
        _norm(alias) for item in selected for alias in item.get("aliases", [])
    }
    if missing:
        raise ValueError(f"Symbols not found in liquidity universe: {', '.join(sorted(missing))}")
    return selected


def batch_universe(instruments: list[dict], batch: int = 1, batch_size: int = 5) -> tuple[list[dict], int]:
    batch_size = max(1, int(batch_size or 5))
    total_batches = max(1, math.ceil(len(instruments) / batch_size))
    batch = max(1, min(int(batch or 1), total_batches))
    start = (batch - 1) * batch_size
    return instruments[start:start + batch_size], total_batches


OPTION_LOT_SIZES = {
    "NIFTY": 65,
    "SENSEX": 20,
    "BANKNIFTY": 30,
    "RELIANCE": 250,
    "HDFCBANK": 550,
    "ICICIBANK": 700,
    "INFY": 300,
    "TCS": 150,
    "SBIN": 1500,
    "AXISBANK": 625,
    "KOTAKBANK": 400,
    "LT": 300,
    "ITC": 1600,
    "BHARTIARTL": 950,
    "HINDUNILVR": 300,
    "BAJFINANCE": 125,
    "HCLTECH": 700,
    "MARUTI": 50,
    "M&M": 150,
    "TATAMOTORS": 1425,
    "TATASTEEL": 5500,
    "SUNPHARMA": 700,
    "ULTRACEMCO": 100,
    "NTPC": 1500,
    "POWERGRID": 3600,
    "ADANIENT": 300,
    "ADANIPORTS": 650,
    "WIPRO": 1500,
    "TECHM": 600,
    "ASIANPAINT": 200,
    "TITAN": 175,
    "BAJAJFINSV": 500,
    "HDFCLIFE": 1100,
    "SBILIFE": 750,
    "COALINDIA": 2100,
    "ONGC": 3850,
    "IOC": 4875,
    "HINDALCO": 1400,
    "JSWSTEEL": 675,
    "CIPLA": 650,
    "DRREDDY": 125,
    "APOLLOHOSP": 125,
    "EICHERMOT": 175,
    "HEROMOTOCO": 150,
    "TATACONSUM": 900,
    "BRITANNIA": 200,
    "NESTLEIND": 400,
    "GRASIM": 475,
    "BAJAJ-AUTO": 75,
}


def get_option_lot_size(symbol: str) -> int:
    return OPTION_LOT_SIZES.get(str(symbol).upper().strip(), 1)


