import datetime
import json
import os
import sqlite3
import time
from contextlib import closing


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TELEMETRY_DB = os.path.join(BASE_DIR, "market_telemetry.db")


def _now(now=None):
    return now or datetime.datetime.now()


def session_date(now=None):
    return _now(now).strftime("%Y-%m-%d")


def _connect():
    conn = sqlite3.connect(TELEMETRY_DB)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema():
    with closing(_connect()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_date TEXT NOT NULL,
                ts REAL NOT NULL,
                time TEXT NOT NULL,
                index_symbol TEXT NOT NULL,
                expiry_date TEXT NOT NULL,
                spot REAL,
                vix REAL,
                atm_strike REAL,
                straddle_premium REAL,
                instant_flow TEXT,
                instant_straddle TEXT,
                cumulative_flow TEXT,
                cumulative_straddle TEXT,
                instant_context_json TEXT,
                cumulative_context_json TEXT,
                oi_flow_snapshot_json TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_market_snapshots_session
            ON market_snapshots(session_date, index_symbol, expiry_date, ts)
            """
        )
        conn.commit()


def _to_json(value):
    return json.dumps(value or {}, separators=(",", ":"), sort_keys=True)


def _from_json(value):
    if not value:
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {}


def _context_dict(context):
    return context.as_dict() if context else {}


def get_session_baseline_snapshot(index_symbol, expiry_date, now=None):
    ensure_schema()
    with closing(_connect()) as conn:
        row = conn.execute(
            """
            SELECT oi_flow_snapshot_json
            FROM market_snapshots
            WHERE session_date = ? AND index_symbol = ? AND expiry_date = ?
              AND oi_flow_snapshot_json IS NOT NULL AND oi_flow_snapshot_json != '{}'
            ORDER BY ts ASC
            LIMIT 1
            """,
            (session_date(now), index_symbol, str(expiry_date)),
        ).fetchone()
    return _from_json(row["oi_flow_snapshot_json"]) if row else None


def get_latest_snapshot(index_symbol, expiry_date, now=None):
    ensure_schema()
    with closing(_connect()) as conn:
        row = conn.execute(
            """
            SELECT oi_flow_snapshot_json
            FROM market_snapshots
            WHERE session_date = ? AND index_symbol = ? AND expiry_date = ?
              AND oi_flow_snapshot_json IS NOT NULL AND oi_flow_snapshot_json != '{}'
            ORDER BY ts DESC
            LIMIT 1
            """,
            (session_date(now), index_symbol, str(expiry_date)),
        ).fetchone()
    return _from_json(row["oi_flow_snapshot_json"]) if row else None


def get_latest_context(index_symbol=None, expiry_date=None, now=None):
    ensure_schema()
    params = [session_date(now)]
    filters = ["session_date = ?"]
    if index_symbol:
        filters.append("index_symbol = ?")
        params.append(index_symbol)
    if expiry_date:
        filters.append("expiry_date = ?")
        params.append(str(expiry_date))

    with closing(_connect()) as conn:
        row = conn.execute(
            f"""
            SELECT *
            FROM market_snapshots
            WHERE {' AND '.join(filters)}
            ORDER BY ts DESC
            LIMIT 1
            """,
            tuple(params),
        ).fetchone()

    if not row:
        return {}

    cumulative = _from_json(row["cumulative_context_json"])
    instant = _from_json(row["instant_context_json"])
    return {
        "session_date": row["session_date"],
        "time": row["time"],
        "index_symbol": row["index_symbol"],
        "expiry_date": row["expiry_date"],
        "spot": row["spot"],
        "vix": row["vix"],
        "instant": instant,
        "cumulative": cumulative or instant,
    }


def record_market_snapshot(index_symbol, expiry_date, spot, vix, instant_context, cumulative_context=None, now=None):
    ensure_schema()
    current = _now(now)
    instant = _context_dict(instant_context)
    cumulative = _context_dict(cumulative_context)
    stored_context = cumulative_context or instant_context
    stored = _context_dict(stored_context)
    snapshot = stored.get("oi_flow_snapshot") or instant.get("oi_flow_snapshot") or {}

    with closing(_connect()) as conn:
        conn.execute(
            """
            INSERT INTO market_snapshots (
                session_date, ts, time, index_symbol, expiry_date, spot, vix,
                atm_strike, straddle_premium, instant_flow, instant_straddle,
                cumulative_flow, cumulative_straddle, instant_context_json,
                cumulative_context_json, oi_flow_snapshot_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_date(current),
                time.mktime(current.timetuple()) + current.microsecond / 1_000_000,
                current.strftime("%Y-%m-%d %H:%M:%S"),
                index_symbol,
                str(expiry_date),
                float(spot or 0.0) if spot is not None else None,
                float(vix or 0.0) if vix is not None else None,
                stored.get("atm_strike"),
                stored.get("straddle_premium"),
                instant.get("flow_signal"),
                instant.get("straddle_signal"),
                cumulative.get("flow_signal"),
                cumulative.get("straddle_signal"),
                _to_json(instant),
                _to_json(cumulative),
                _to_json(snapshot),
            ),
        )
        conn.commit()


def reset_session(index_symbol=None, expiry_date=None, now=None):
    ensure_schema()
    params = [session_date(now)]
    filters = ["session_date = ?"]
    if index_symbol:
        filters.append("index_symbol = ?")
        params.append(index_symbol)
    if expiry_date:
        filters.append("expiry_date = ?")
        params.append(str(expiry_date))

    with closing(_connect()) as conn:
        conn.execute(
            f"DELETE FROM market_snapshots WHERE {' AND '.join(filters)}",
            tuple(params),
        )
        conn.commit()
