# config.py
import json
import os

# --- ABSOLUTE PATHING ---
# Automatically finds the exact folder this script is sitting in
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r") as f:
            return json.load(f)
    return {}

settings = load_settings()

# Remove the old static LIVE_ACCESS_TOKEN variable and replace it with this:

def get_live_token():
    """Dynamically reads the freshest token from the file directly from the hard drive."""
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                return json.load(f).get("LIVE_ACCESS_TOKEN", "")
        except Exception:
            pass
    return ""

def get_target_profit_pct():
    """Dynamically reads the profit target from the UI so it can be changed mid-trade."""
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                return json.load(f).get("TARGET_PROFIT_PCT", 20)
        except Exception:
            pass
    return 20 # Safe default



#LIVE_ACCESS_TOKEN = settings.get("LIVE_ACCESS_TOKEN", "")
#SANDBOX_ACCESS_TOKEN = settings.get("eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ.eyJzdWIiOiI0MDE0MzAiLCJqdGkiOiI2OWNjMGUwMDdlZjliNjZjZTI3MGFjMWQiLCJpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6dHJ1ZSwiaWF0IjoxNzc0OTgwNjA4LCJpc3MiOiJ1ZGFwaS1nYXRld2F5LXNlcnZpY2UiLCJleHAiOjE3Nzc1MDAwMDB9.0HW6tC9xWuz-col0AOjsgBRVTDvGV6ixFU2Vn73oj3U", "")

SANDBOX_ACCESS_TOKEN = settings.get("SANDBOX_ACCESS_TOKEN", "")

NIFTY_EXPIRY = settings.get("NIFTY_EXPIRY", "")
SENSEX_EXPIRY = settings.get("SENSEX_EXPIRY", "")

#NIFTY_LOT_SIZE = settings.get("NIFTY_LOT_SIZE", 65)
#SENSEX_LOT_SIZE = settings.get("SENSEX_LOT_SIZE", 20)

# In config.py, replace the static lot sizes with these functions:

def get_nifty_qty():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                return json.load(f).get("NIFTY_LOT_SIZE", 65)
        except Exception: pass
    return 65

def get_sensex_qty():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                return json.load(f).get("SENSEX_LOT_SIZE", 20)
        except Exception: pass
    return 20


ENVIRONMENT = settings.get("ENVIRONMENT", "SANDBOX")

WING_PERCENT = 0.05  # Legacy fallback — now overridden by delta-based wing selection
MAX_DELTA_SKEW = 0.15

# --- VIX ADAPTIVE PROFILE THRESHOLDS ---
VIX_LOW_THRESHOLD = 13    # Below this → tighten wings (8δ), conservative 12% target
VIX_HIGH_THRESHOLD = 18   # Above this → widen wings (12δ), aggressive 25% target

# --- OPENING RANGE GAP FILTER ---
GAP_THRESHOLD_PCT = 0.008   # 0.8% gap triggers a cooldown pause
GAP_SETTLE_MINUTES = 15     # Wait 15 minutes for gap to absorb

# --- CIRCUIT BREAKER ---
MAX_CONSECUTIVE_LOSSES = 2  # Halt session after 2 consecutive losses

# --- ATM DRIFT GUARD (Stale Strike) ---
ATM_DRIFT_MULTIPLIER = 1.5  # Exit if spot drifts > 1.5x wing width from ATM

# --- RATCHET TRAILING STOP ---
TRAIL_LOCK_FLOOR_PCT = 0.80   # Lock 80% of base target when trail activates
TRAIL_RATCHET_FACTOR = 0.75   # Lock 75% of the high-water mark as it climbs


# --- MARKET HOLIDAYS (YYYY-MM-DD) ---
# The bot will completely ignore trading on these dates.
MARKET_HOLIDAYS = [
    "2026-04-14",  # Dr. Ambedkar Jayanti
    "2026-05-01",  # Maharashtra Day
    "2026-08-15",  # Independence Day
    # Add any other official exchange holidays here...
]

TELEGRAM_BOT_TOKEN = "8335051930:AAFTA7WvOcIEvjgEDwA1YTenKwARNkibdKE" 
TELEGRAM_CHAT_ID = "635369910"
