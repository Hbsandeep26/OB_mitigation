"""Fetch six-month intraday candles from Dhan in small logged batches."""

from __future__ import annotations

import argparse
import csv
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import config
from liquidity_universe import batch_universe, select_universe


BASE_URL = "https://api.dhan.co/v2"


def setup_logging(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "fetch_historical_data.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Dhan intraday candles for liquid instruments.")
    parser.add_argument("--symbols", help="Comma-separated symbols. Default: full liquid universe.")
    parser.add_argument("--universe-file", help="Optional JSON universe override.")
    parser.add_argument("--batch", type=int, default=1, help="1-based batch number to run.")
    parser.add_argument("--batch-size", type=int, default=5, help="Symbols per execution.")
    parser.add_argument("--months", type=int, default=6, help="Lookback months when from-date is omitted.")
    parser.add_argument("--from-date", help="YYYY-MM-DD. Default: roughly --months back.")
    parser.add_argument("--to-date", help="YYYY-MM-DD. Default: today.")
    parser.add_argument("--interval", type=int, default=5, help="Dhan intraday interval in minutes.")
    parser.add_argument("--chunk-days", type=int, default=30, help="Days per API request chunk.")
    parser.add_argument("--output-dir", default="data/historical", help="Directory for CSV output.")
    parser.add_argument("--sleep", type=float, default=0.35, help="Delay between API calls.")
    parser.add_argument("--skip-existing", action="store_true", help="Skip a symbol if its CSV already exists.")
    parser.add_argument("--dry-run", action="store_true", help="Print selected batch without calling Dhan.")
    return parser.parse_args()


def dhan_headers() -> dict:
    token = config.get_dhan_access_token()
    client_id = config.get_dhan_client_id()
    if not token or not client_id:
        raise ValueError("DHAN_ACCESS_TOKEN and DHAN_CLIENT_ID must be set in settings.json or environment")
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "access-token": token,
        "client-id": client_id,
        "dhanClientId": client_id,
    }


def parse_date_range(args: argparse.Namespace) -> tuple[datetime, datetime]:
    if args.to_date:
        to_dt = datetime.strptime(args.to_date, "%Y-%m-%d")
    else:
        to_dt = datetime.now()

    if args.from_date:
        from_dt = datetime.strptime(args.from_date, "%Y-%m-%d")
    else:
        from_dt = to_dt - timedelta(days=max(1, args.months) * 31)

    from_dt = from_dt.replace(hour=9, minute=15, second=0, microsecond=0)
    to_dt = to_dt.replace(hour=15, minute=30, second=0, microsecond=0)
    return from_dt, to_dt


def date_chunks(from_dt: datetime, to_dt: datetime, chunk_days: int):
    chunk_days = max(1, int(chunk_days or 30))
    cursor = from_dt
    while cursor <= to_dt:
        end = min(cursor + timedelta(days=chunk_days), to_dt)
        yield cursor, end
        cursor = end + timedelta(seconds=1)


def fetch_chunk(instrument: dict, from_dt: datetime, to_dt: datetime, interval: int) -> dict:
    try:
        import requests
    except ImportError as err:
        raise RuntimeError("The requests package is required for live Dhan data fetching") from err

    payload = {
        "securityId": str(instrument["security_id"]),
        "exchangeSegment": instrument["exchange_segment"],
        "instrument": instrument.get("instrument", "EQUITY"),
        "interval": str(int(interval)),
        "oi": True,
        "fromDate": from_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "toDate": to_dt.strftime("%Y-%m-%d %H:%M:%S"),
    }
    response = requests.post(
        f"{BASE_URL}/charts/intraday",
        headers=dhan_headers(),
        json=payload,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def timestamp_to_text(value) -> str:
    if value in (None, ""):
        return ""
    text = str(value)
    try:
        number = float(text)
    except ValueError:
        return text
    if number > 10_000_000_000:
        number /= 1000.0
    try:
        return datetime.fromtimestamp(number).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError):
        return text


def rows_from_payload(symbol: str, payload: dict) -> list[dict]:
    timestamps = payload.get("timestamp", []) or []
    opens = payload.get("open", []) or []
    highs = payload.get("high", []) or []
    lows = payload.get("low", []) or []
    closes = payload.get("close", []) or []
    volumes = payload.get("volume", []) or []
    open_interest = payload.get("open_interest", []) or []

    rows = []
    for idx, ts in enumerate(timestamps):
        try:
            rows.append({
                "symbol": symbol,
                "timestamp": ts,
                "datetime": timestamp_to_text(ts),
                "open": float(opens[idx]),
                "high": float(highs[idx]),
                "low": float(lows[idx]),
                "close": float(closes[idx]),
                "volume": float(volumes[idx]) if idx < len(volumes) else 0.0,
                "open_interest": float(open_interest[idx]) if idx < len(open_interest) else 0.0,
            })
        except (IndexError, TypeError, ValueError):
            continue
    return rows


def write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    deduped = {str(row["timestamp"]): row for row in rows}
    ordered = sorted(deduped.values(), key=lambda row: str(row["timestamp"]))
    fieldnames = ["symbol", "timestamp", "datetime", "open", "high", "low", "close", "volume", "open_interest"]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(ordered)


def fetch_symbol(instrument: dict, args: argparse.Namespace, from_dt: datetime, to_dt: datetime, output_dir: Path) -> Path:
    symbol = instrument["symbol"]
    output_path = output_dir / f"{symbol}_{args.interval}m.csv"
    if args.skip_existing and output_path.exists():
        logging.info("[%s] skipped because %s already exists", symbol, output_path)
        return output_path

    all_rows = []
    chunks = list(date_chunks(from_dt, to_dt, args.chunk_days))
    logging.info("[%s] fetching %s chunks from %s to %s", symbol, len(chunks), from_dt.date(), to_dt.date())
    for idx, (chunk_start, chunk_end) in enumerate(chunks, start=1):
        logging.info("[%s] chunk %s/%s: %s -> %s", symbol, idx, len(chunks), chunk_start, chunk_end)
        payload = fetch_chunk(instrument, chunk_start, chunk_end, args.interval)
        rows = rows_from_payload(symbol, payload)
        all_rows.extend(rows)
        logging.info("[%s] chunk %s produced %s candles; accumulated=%s", symbol, idx, len(rows), len(all_rows))
        time.sleep(max(0.0, args.sleep))

    write_rows(output_path, all_rows)
    logging.info("[%s] wrote %s unique candles to %s", symbol, len({str(r["timestamp"]) for r in all_rows}), output_path)
    return output_path


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    setup_logging(output_dir)
    instruments = select_universe(args.symbols, args.universe_file)
    selected, total_batches = batch_universe(instruments, batch=args.batch, batch_size=args.batch_size)
    from_dt, to_dt = parse_date_range(args)
    logging.info(
        "Selected batch %s/%s with %s instruments: %s",
        args.batch,
        total_batches,
        len(selected),
        ", ".join(item["symbol"] for item in selected),
    )

    if args.dry_run:
        return 0

    failures = []
    for pos, instrument in enumerate(selected, start=1):
        logging.info("Starting instrument %s/%s: %s", pos, len(selected), instrument["symbol"])
        try:
            fetch_symbol(instrument, args, from_dt, to_dt, output_dir)
        except Exception as err:
            logging.exception("[%s] fetch failed: %s", instrument["symbol"], err)
            failures.append(instrument["symbol"])

    if failures:
        logging.error("Fetch completed with failures: %s", ", ".join(failures))
        return 1

    logging.info("Fetch completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
