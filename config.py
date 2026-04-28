import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")


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

# --- MARKET HOLIDAYS (YYYY-MM-DD) ---
MARKET_HOLIDAYS = [
    "2026-04-14",
    "2026-05-01",
    "2026-08-15",
]

import datetime

def get_next_expiry(index_symbol):
    now = datetime.datetime.now()
    today = now.date()
    target_weekday = 3 if index_symbol == "NIFTY" else 4
    
    for i in range(14):
        test_date = today + datetime.timedelta(days=i)
        days_to_target = target_weekday - test_date.weekday()
        nominal_expiry = test_date + datetime.timedelta(days=days_to_target)
        
        actual_expiry = nominal_expiry
        while True:
            date_str = actual_expiry.strftime("%Y-%m-%d")
            is_weekend = actual_expiry.weekday() >= 5
            is_holiday = date_str in MARKET_HOLIDAYS
            if not is_weekend and not is_holiday:
                break
            actual_expiry -= datetime.timedelta(days=1)
            
        if actual_expiry > today:
            return actual_expiry.strftime("%Y-%m-%d")
        elif actual_expiry == today and now.hour < 16:
            return actual_expiry.strftime("%Y-%m-%d")
            
    return today.strftime("%Y-%m-%d")

NIFTY_EXPIRY = settings.get("NIFTY_EXPIRY", get_next_expiry("NIFTY"))
SENSEX_EXPIRY = settings.get("SENSEX_EXPIRY", get_next_expiry("SENSEX"))


def get_nifty_qty():
    return _int_setting("NIFTY_LOT_SIZE", 65)


def get_sensex_qty():
    return _int_setting("SENSEX_LOT_SIZE", 20)


ENVIRONMENT = settings.get("ENVIRONMENT", "SANDBOX")

WING_PERCENT = 0.05
MAX_DELTA_SKEW = 0.15
NIFTY_LOT_MULTIPLE = 65
SENSEX_LOT_MULTIPLE = 20

# --- VIX ADAPTIVE PROFILE THRESHOLDS ---
VIX_LOW_THRESHOLD = 13
VIX_HIGH_THRESHOLD = 18

# --- OPENING RANGE GAP FILTER ---
GAP_THRESHOLD_PCT = 0.008
GAP_SETTLE_MINUTES = 15

# --- CIRCUIT BREAKER ---
MAX_CONSECUTIVE_LOSSES = 2

# --- EXECUTION SAFETY ---
ORDER_CONFIRM_TIMEOUT_SECONDS = _float_setting("ORDER_CONFIRM_TIMEOUT_SECONDS", 12.0)
ORDER_CONFIRM_POLL_SECONDS = _float_setting("ORDER_CONFIRM_POLL_SECONDS", 0.5)
MAX_SOCKET_RECONNECTS = _int_setting("MAX_SOCKET_RECONNECTS", 3)
MAX_FEED_STALENESS_SECONDS = _float_setting("MAX_FEED_STALENESS_SECONDS", 5.0)
WEBSOCKET_SILENT_SECONDS = _float_setting("WEBSOCKET_SILENT_SECONDS", 60.0)
HEARTBEAT_INTERVAL_SECONDS = _float_setting("HEARTBEAT_INTERVAL_SECONDS", 5.0)
NET_STOP_LOSS_MULTIPLIER = _float_setting("NET_STOP_LOSS_MULTIPLIER", 2.0)
MAX_DEFINED_LOSS_RUPEES = _float_setting("MAX_DEFINED_LOSS_RUPEES", 0.0)

# --- ATM DRIFT GUARD ---
ATM_DRIFT_MULTIPLIER = 1.5

# --- RATCHET TRAILING STOP ---
TRAIL_LOCK_FLOOR_PCT = 0.80
TRAIL_RATCHET_FACTOR = 0.85  # Tighter: keep 85% of peak (was 75%)

# --- PROGRESSIVE PROFIT LOCK TIERS ---
PROFIT_LOCK_TIER1_TRIGGER = 0.40   # 40% of target
PROFIT_LOCK_TIER1_FLOOR = 0.15     # Lock 15% of target
PROFIT_LOCK_TIER2_TRIGGER = 0.60   # 60% of target
PROFIT_LOCK_TIER2_FLOOR = 0.35     # Lock 35% of target
PROFIT_LOCK_TIER3_TRIGGER = 0.80   # 80% of target
PROFIT_LOCK_TIER3_FLOOR = 0.60     # Lock 60% of target

# --- BTST HEALTH CHECK ---
BTST_MAX_SKEW_RATIO = 2.0          # Max CE/PE ratio for healthy BTST
BTST_MIN_LEG_PCT = 0.30            # Min premium retention per leg (30%)
BTST_RECENTER_CUTOFF_MINUTE = 20   # Latest minute past 15:xx to allow recenter

# Prefer environment variables for secrets. settings.json remains supported for
# local-only use, but credentials are no longer hard-coded in source.
TELEGRAM_BOT_TOKEN = _setting("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = _setting("TELEGRAM_CHAT_ID", "")
