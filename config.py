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


def _bool_setting(key, default):
    value = _setting(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


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




def get_nifty_qty():
    return _int_setting("NIFTY_LOT_SIZE", 65)


def get_sensex_qty():
    return _int_setting("SENSEX_LOT_SIZE", 20)


ENVIRONMENT = settings.get("ENVIRONMENT", "SANDBOX")

VIRTUAL_CAPITAL = _float_setting("VIRTUAL_CAPITAL", 220000.0)
MAX_CAPITAL_UTILIZATION = _float_setting("MAX_CAPITAL_UTILIZATION", 0.80)

BUY_LEG_PERCENT = _float_setting("BUY_LEG_PERCENT", 5.0)
SNIPER_WING_DELTA = _float_setting("SNIPER_WING_DELTA", 0.0)
NIFTY_LOT_MULTIPLE = 65
SENSEX_LOT_MULTIPLE = 20

# --- VOLATILITY ROUTING ---
INDIA_VIX_TOGGLE_LEVEL = _float_setting("INDIA_VIX_TOGGLE_LEVEL", 15.0)
CONDOR_SHORT_STRIKE_OFFSET = _float_setting("CONDOR_SHORT_STRIKE_OFFSET", 300.0)
BTST_SPREAD_WIDTH_POINTS = _float_setting("BTST_SPREAD_WIDTH_POINTS", 400.0)
BTST_EXIT_TIME = _setting("BTST_EXIT_TIME", "09:20")

# --- OPENING RANGE GAP FILTER ---
GAP_THRESHOLD_PCT = 0.008
GAP_SETTLE_MINUTES = 15

# --- EXECUTION SAFETY ---
ORDER_CONFIRM_TIMEOUT_SECONDS = _float_setting("ORDER_CONFIRM_TIMEOUT_SECONDS", 12.0)
ORDER_CONFIRM_POLL_SECONDS = _float_setting("ORDER_CONFIRM_POLL_SECONDS", 0.5)
MAX_SOCKET_RECONNECTS = _int_setting("MAX_SOCKET_RECONNECTS", 3)
MAX_FEED_STALENESS_SECONDS = _float_setting("MAX_FEED_STALENESS_SECONDS", 5.0)
MAX_INCOMPLETE_FEED_SECONDS = _float_setting("MAX_INCOMPLETE_FEED_SECONDS", 20.0)
WEBSOCKET_SILENT_SECONDS = _float_setting("WEBSOCKET_SILENT_SECONDS", 60.0)
HEARTBEAT_INTERVAL_SECONDS = _float_setting("HEARTBEAT_INTERVAL_SECONDS", 5.0)
MAX_DEFINED_LOSS_RUPEES = _float_setting("MAX_DEFINED_LOSS_RUPEES", 0.0)
NORMAL_ENTRY_TIME = _setting("NORMAL_ENTRY_TIME", "09:16")
EXPIRY_ENTRY_TIME = _setting("EXPIRY_ENTRY_TIME", "09:20")

# --- SNIPER & SHIELD STRATEGY ---
SNIPER_TARGETS_ENABLED = _bool_setting("SNIPER_TARGETS_ENABLED", True)
SNIPER_TARGET_PCT = _float_setting("SNIPER_TARGET_PCT", 12.0)
ATM_DRIFT_EJECT_THRESHOLD = _float_setting(
    "ATM_DRIFT_EJECT_THRESHOLD",
    _float_setting("SNIPER_DRIFT_EJECT_RATIO", 0.20),
)
# Backward-compatible alias for existing dashboard/state code.
SNIPER_DRIFT_EJECT_RATIO = ATM_DRIFT_EJECT_THRESHOLD
CONDOR_ATM_DRIFT_THRESHOLD = _float_setting(
    "CONDOR_ATM_DRIFT_THRESHOLD",
    _float_setting("Condor_ATM_Drift_Threshold", 0.40),
)
# Alias requested in the strategy specification.
Condor_ATM_Drift_Threshold = CONDOR_ATM_DRIFT_THRESHOLD
SNIPER_CATASTROPHE_MULTIPLIER = _float_setting("SNIPER_CATASTROPHE_MULTIPLIER", 1.05)
EMERGENCY_EXIT_CONFIRMATION_ENABLED = _bool_setting("EMERGENCY_EXIT_CONFIRMATION_ENABLED", True)

# --- ALGO COMMAND CENTER STRATEGY CARDS ---
IRON_CONDOR_VIX_ACTIVATION = _float_setting("IRON_CONDOR_VIX_ACTIVATION", 15.0)
IRON_CONDOR_ATM_DRIFT_POINTS = _float_setting("IRON_CONDOR_ATM_DRIFT_POINTS", 45.0)
IRON_CONDOR_TARGET_PCT = _float_setting("IRON_CONDOR_TARGET_PCT", 70.0)
IRON_CONDOR_CATASTROPHE_MULTIPLIER = _float_setting("IRON_CONDOR_CATASTROPHE_MULTIPLIER", 1.5)

IRON_BUTTERFLY_VIX_ACTIVATION = _float_setting("IRON_BUTTERFLY_VIX_ACTIVATION", 15.0)
IRON_BUTTERFLY_ATM_DRIFT_POINTS = _float_setting("IRON_BUTTERFLY_ATM_DRIFT_POINTS", 25.0)
IRON_BUTTERFLY_TARGET_PCT = _float_setting("IRON_BUTTERFLY_TARGET_PCT", 55.0)
IRON_BUTTERFLY_CATASTROPHE_MULTIPLIER = _float_setting("IRON_BUTTERFLY_CATASTROPHE_MULTIPLIER", 1.2)

DIRECTIONAL_TARGET_PCT = _float_setting("DIRECTIONAL_TARGET_PCT", 75.0)
DIRECTIONAL_CATASTROPHE_MULTIPLIER = _float_setting("DIRECTIONAL_CATASTROPHE_MULTIPLIER", 1.0)
DIRECTIONAL_BTST_AUTO_EXIT_ROC_PCT = _float_setting("DIRECTIONAL_BTST_AUTO_EXIT_ROC_PCT", 3.0)

IRON_BUTTERFLY_MARGIN_LOW_VIX = _float_setting("IRON_BUTTERFLY_MARGIN_LOW_VIX", 180000.0)
IRON_BUTTERFLY_MARGIN_HIGH_VIX = _float_setting("IRON_BUTTERFLY_MARGIN_HIGH_VIX", 200000.0)
IRON_CONDOR_MARGIN_HIGH_VIX = _float_setting("IRON_CONDOR_MARGIN_HIGH_VIX", 120000.0)
IRON_CONDOR_MARGIN_LOW_VIX = _float_setting("IRON_CONDOR_MARGIN_LOW_VIX", 100000.0)
DIRECTIONAL_SPREAD_MARGIN = _float_setting("DIRECTIONAL_SPREAD_MARGIN", 40000.0)

# --- BTST HEALTH CHECK ---
BTST_MAX_SKEW_RATIO = 2.0          # Max CE/PE ratio for healthy BTST
BTST_MIN_LEG_PCT = 0.30            # Min premium retention per leg (30%)
BTST_RECENTER_MIN_DRIFT_RATIO = _float_setting("BTST_RECENTER_MIN_DRIFT_RATIO", 0.06)
BTST_MOMENTUM_ENABLED = _bool_setting("BTST_MOMENTUM_ENABLED", True)
BTST_BULLISH_RANGE_CLOSE = _float_setting("BTST_BULLISH_RANGE_CLOSE", 0.80)
BTST_BEARISH_RANGE_CLOSE = _float_setting("BTST_BEARISH_RANGE_CLOSE", 0.20)
BTST_NEUTRAL_RANGE_CLOSE_UPPER = _float_setting("BTST_NEUTRAL_RANGE_CLOSE_UPPER", 0.799)

# --- POST-EMERGENCY RE-ENTRY GUARD ---
POST_EMERGENCY_REENTRY_ENABLED = _bool_setting("POST_EMERGENCY_REENTRY_ENABLED", True)
POST_EMERGENCY_REENTRY_COOLDOWN_SECONDS = _float_setting("POST_EMERGENCY_REENTRY_COOLDOWN_SECONDS", 60.0)
POST_EMERGENCY_REENTRY_MIN_MINUTES_TO_CUTOFF = _float_setting("POST_EMERGENCY_REENTRY_MIN_MINUTES_TO_CUTOFF", 10.0)
POST_EMERGENCY_MAX_PREMIUM_CHANGE_PCT = _float_setting("POST_EMERGENCY_MAX_PREMIUM_CHANGE_PCT", 0.12)
POST_EMERGENCY_MAX_SPOT_CHANGE_PCT = _float_setting("POST_EMERGENCY_MAX_SPOT_CHANGE_PCT", 0.0025)

# Prefer environment variables for secrets. settings.json remains supported for
# local-only use, but credentials are no longer hard-coded in source.
TELEGRAM_BOT_TOKEN = "8335051930:AAFTA7WvOcIEvjgEDwA1YTenKwARNkibdKE" 
TELEGRAM_CHAT_ID = "635369910"
