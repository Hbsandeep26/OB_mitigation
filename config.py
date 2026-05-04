import json
import os
import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")
EXPIRIES_FILE = os.path.join(BASE_DIR, "expiries.json")


def load_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r") as f:
            return json.load(f)
    return {}


settings = load_settings()


def _setting(key, default=None):
    """Read the latest value from environment first, then settings.json."""
    env_value = os.getenv(key)
    if env_value not in (None, ""):
        return env_value

    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                return json.load(f).get(key, default)
        except Exception:
            pass
    return default


def _float_setting(key, default):
    try:
        return float(_setting(key, default))
    except (TypeError, ValueError):
        return float(default)


def _int_setting(key, default):
    try:
        return int(_setting(key, default))
    except (TypeError, ValueError):
        return int(default)


def get_live_token():
    """Dynamically reads the freshest token from environment/settings."""
    return _setting("LIVE_ACCESS_TOKEN", "")


def get_target_profit_pct():
    """Dynamically reads the profit target from the UI."""
    return _float_setting("TARGET_PROFIT_PCT", 20)


SANDBOX_ACCESS_TOKEN = settings.get("SANDBOX_ACCESS_TOKEN", "")


def _parse_date(date_str):
    return datetime.datetime.strptime(str(date_str), "%Y-%m-%d").date()


def load_expiry_calendar():
    """Load manually maintained weekly expiries and holidays.

    Supports the old {"NIFTY": "YYYY-MM-DD"} shape and the new
    {"NIFTY": ["YYYY-MM-DD"], "SENSEX": [...], "HOLIDAYS": [...]} shape.
    """
    data = {}
    if os.path.exists(EXPIRIES_FILE):
        try:
            with open(EXPIRIES_FILE, "r") as f:
                data = json.load(f)
        except Exception:
            data = {}

    def normalize_list(key):
        value = data.get(key, [])
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            value = []
        valid = []
        for item in value:
            try:
                valid.append(_parse_date(item).strftime("%Y-%m-%d"))
            except (TypeError, ValueError):
                continue
        return sorted(set(valid))

    return {
        "NIFTY": normalize_list("NIFTY"),
        "SENSEX": normalize_list("SENSEX"),
        "HOLIDAYS": normalize_list("HOLIDAYS") or normalize_list("MARKET_HOLIDAYS"),
    }


def get_market_holidays():
    calendar = load_expiry_calendar()
    fallback = settings.get("MARKET_HOLIDAYS", [])
    if isinstance(fallback, str):
        fallback = [fallback]
    return sorted(set(calendar["HOLIDAYS"] + [str(day) for day in fallback]))


MARKET_HOLIDAYS = get_market_holidays()


def get_next_expiry(index_symbol, now=None):
    now = now or datetime.datetime.now()
    today = now.date()
    calendar = load_expiry_calendar()
    expiries = calendar.get(index_symbol, [])
    holidays = set(calendar.get("HOLIDAYS", []))

    for expiry in expiries:
        try:
            expiry_date = _parse_date(expiry)
        except ValueError:
            continue
        if expiry in holidays or expiry_date.weekday() >= 5:
            continue
        if expiry_date > today or (expiry_date == today and now.time() < datetime.time(16, 0)):
            return expiry

    configured = settings.get(f"{index_symbol}_EXPIRY")
    if configured:
        return configured
    return "UNKNOWN"


def validate_expiry_calendar(now=None):
    now = now or datetime.datetime.now()
    errors = []
    calendar = load_expiry_calendar()
    today = now.date()

    for index_symbol in ("NIFTY", "SENSEX"):
        expiries = calendar.get(index_symbol, [])
        if not expiries:
            errors.append(f"{index_symbol}: no weekly expiries configured in expiries.json")
            continue
        next_expiry = get_next_expiry(index_symbol, now)
        if next_expiry == "UNKNOWN":
            errors.append(f"{index_symbol}: no valid future weekly expiry found in expiries.json")
            continue
        try:
            if _parse_date(next_expiry) < today:
                errors.append(f"{index_symbol}: next expiry {next_expiry} is stale")
        except ValueError:
            errors.append(f"{index_symbol}: next expiry {next_expiry} is invalid")
    return errors


NIFTY_EXPIRY = get_next_expiry("NIFTY")
SENSEX_EXPIRY = get_next_expiry("SENSEX")


def get_nifty_qty():
    return _int_setting("NIFTY_LOT_SIZE", 65)


def get_sensex_qty():
    return _int_setting("SENSEX_LOT_SIZE", 20)


ENVIRONMENT = settings.get("ENVIRONMENT", "SANDBOX")

WING_PERCENT = 0.05
NIFTY_LOT_MULTIPLE = 65
SENSEX_LOT_MULTIPLE = 20

# --- OPENING RANGE GAP FILTER ---
GAP_THRESHOLD_PCT = 0.008
GAP_SETTLE_MINUTES = 15

# --- EXECUTION SAFETY ---
ORDER_CONFIRM_TIMEOUT_SECONDS = _float_setting("ORDER_CONFIRM_TIMEOUT_SECONDS", 12.0)
ORDER_CONFIRM_POLL_SECONDS = _float_setting("ORDER_CONFIRM_POLL_SECONDS", 0.5)
MAX_SOCKET_RECONNECTS = _int_setting("MAX_SOCKET_RECONNECTS", 3)
MAX_FEED_STALENESS_SECONDS = _float_setting("MAX_FEED_STALENESS_SECONDS", 5.0)
WEBSOCKET_SILENT_SECONDS = _float_setting("WEBSOCKET_SILENT_SECONDS", 60.0)
HEARTBEAT_INTERVAL_SECONDS = _float_setting("HEARTBEAT_INTERVAL_SECONDS", 5.0)
MAX_DEFINED_LOSS_RUPEES = _float_setting("MAX_DEFINED_LOSS_RUPEES", 0.0)
NORMAL_ENTRY_TIME = _setting("NORMAL_ENTRY_TIME", "09:16")
EXPIRY_ENTRY_TIME = _setting("EXPIRY_ENTRY_TIME", "09:20")

# --- SNIPER & SHIELD STRATEGY ---
SNIPER_WING_DELTA = _float_setting("SNIPER_WING_DELTA", 5.0)
SNIPER_TARGET_PCT = _float_setting("SNIPER_TARGET_PCT", 12.0)
SNIPER_LEVEL_UP_TARGET_PCT = _float_setting("SNIPER_LEVEL_UP_TARGET_PCT", 18.0)
SNIPER_LEVEL_UP_FLOOR_PCT = _float_setting("SNIPER_LEVEL_UP_FLOOR_PCT", 10.0)
SNIPER_DRIFT_EJECT_RATIO = _float_setting("SNIPER_DRIFT_EJECT_RATIO", 0.35)
SNIPER_PINNED_DRIFT_RATIO = _float_setting("SNIPER_PINNED_DRIFT_RATIO", 0.20)
SNIPER_CATASTROPHE_MULTIPLIER = _float_setting("SNIPER_CATASTROPHE_MULTIPLIER", 1.20)

# --- BTST HEALTH CHECK ---
BTST_MAX_SKEW_RATIO = 2.0          # Max CE/PE ratio for healthy BTST
BTST_MIN_LEG_PCT = 0.30            # Min premium retention per leg (30%)

# Prefer environment variables for secrets. settings.json remains supported for
# local-only use, but credentials are no longer hard-coded in source.
TELEGRAM_BOT_TOKEN = _setting("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = _setting("TELEGRAM_CHAT_ID", "")
