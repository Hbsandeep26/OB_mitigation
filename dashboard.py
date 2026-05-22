# dashboard.py
import streamlit as st
import pandas as pd
import json
import os
import time
import subprocess
import sys
import requests
import urllib.parse
import psutil
import datetime
import config
import telemetry
import uuid

try:
    import plotly.graph_objects as go
except ImportError:
    go = None

st.set_page_config(page_title="Algo Command Center", layout="wide")

# --- PREMIUM UI/UX CSS ---
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap');
    html, body, [class*="css"] {
        font-family: 'Inter', sans-serif;
    }
    .stApp {
        background-color: #0f172a;
        color: #f8fafc;
    }
    div[data-testid="stMetric"] {
        background: rgba(30, 41, 59, 0.7);
        backdrop-filter: blur(10px);
        border: 1px solid rgba(255, 255, 255, 0.1);
        border-radius: 12px;
        padding: 15px;
        box-shadow: 0 4px 6px rgba(0, 0, 0, 0.3);
    }
    div[data-testid="stDataFrame"] {
        border-radius: 12px;
        overflow: hidden;
        box-shadow: 0 4px 6px rgba(0, 0, 0, 0.3);
    }
    h1, h2, h3 {
        color: #e2e8f0 !important;
        font-weight: 800 !important;
    }
    .stButton>button {
        border-radius: 8px;
        transition: all 0.3s ease;
    }
    .stButton>button:hover {
        transform: translateY(-2px);
        box-shadow: 0 4px 12px rgba(59, 130, 246, 0.5);
    }
    .muted-header {
        color: #94a3b8;
        font-size: 0.82rem;
        margin-bottom: 0.4rem;
    }
    .market-tile {
        background: rgba(15, 23, 42, 0.72);
        border: 1px solid rgba(148, 163, 184, 0.22);
        border-radius: 8px;
        padding: 14px 16px;
        min-height: 92px;
    }
    .market-label {
        color: #94a3b8;
        font-size: 0.76rem;
        font-weight: 600;
        letter-spacing: 0;
        text-transform: uppercase;
    }
    .market-value {
        color: #f8fafc;
        font-size: 1.45rem;
        font-weight: 800;
        line-height: 1.25;
        margin-top: 3px;
    }
    .tone-bullish { color: #22c55e; }
    .tone-bearish { color: #ef4444; }
    .tone-neutral { color: #f59e0b; }
    .tone-muted { color: #cbd5e1; }
</style>
""", unsafe_allow_html=True)

# --- ABSOLUTE PATHING ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")
PID_FILE = os.path.join(BASE_DIR, "engine_pid.txt")
STATE_FILE = os.path.join(BASE_DIR, "trade_state.json")
LOG_FILE_PATH = os.path.join(BASE_DIR, "bot.log")
CSV_LOG_FILE = os.path.join(BASE_DIR, "sandbox_trade_logs.csv")
BTST_FILE = os.path.join(BASE_DIR, "btst_flag.txt")
PANIC_FILE = os.path.join(BASE_DIR, "panic_flag.txt")
LIVE_FILE = os.path.join(BASE_DIR, "live_prices.json")
HEARTBEAT_FILE = os.path.join(BASE_DIR, "engine_heartbeat.json")
GRACEFUL_STOP_FILE = os.path.join(BASE_DIR, "graceful_stop_flag.txt")
MANUAL_EXIT_FILE = os.path.join(BASE_DIR, "manual_exit_flag.txt")
MANUAL_ENTRY_FILE = os.path.join(BASE_DIR, "manual_entry_flag.txt")

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_settings(new_settings):
    atomic_write_json(SETTINGS_FILE, new_settings)

def atomic_write_json(path, data):
    temp_path = f"{path}.{uuid.uuid4().hex[:8]}.tmp"
    with open(temp_path, "w") as f:
        json.dump(data, f, indent=4)
    for _ in range(5):
        try:
            os.replace(temp_path, path)
            break
        except PermissionError:
            time.sleep(0.05)

def atomic_write_text(path, text):
    temp_path = f"{path}.{uuid.uuid4().hex[:8]}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        f.write(text)
    for _ in range(5):
        try:
            os.replace(temp_path, path)
            break
        except PermissionError:
            time.sleep(0.05)

def read_pid():
    if not os.path.exists(PID_FILE):
        return None
    try:
        with open(PID_FILE, "r") as f:
            return int(f.read().strip())
    except Exception:
        return None


def remove_flag(path):
    if os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass

def heartbeat_age():
    if not os.path.exists(HEARTBEAT_FILE):
        return None, {}
    try:
        with open(HEARTBEAT_FILE, "r") as f:
            heartbeat = json.load(f)
        return time.time() - float(heartbeat.get("ts", 0)), heartbeat
    except Exception:
        return None, {}


def order_sequence_for_state(state):
    execution_info = state.get("execution_info", {}) if state else {}
    sequence = []
    for item in execution_info.get("order_sequence", []):
        if len(item) == 2:
            sequence.append((str(item[0]), str(item[1]).upper()))
    if not sequence:
        for leg_name in (state or {}).get("legs", {}):
            sequence.append((leg_name, "SELL" if leg_name.startswith("sell") else "BUY"))
    return sequence


def net_from_sequence(prices, sequence):
    net = 0.0
    for leg_name, transaction_type in sequence:
        price = float(prices.get(leg_name, 0.0) or 0.0)
        net += price if transaction_type == "SELL" else -price
    return net


def pnl_from_sequence(entries, exits, qty, sequence):
    pnl = 0.0
    for leg_name, transaction_type in sequence:
        entry_price = float(entries.get(leg_name, 0.0) or 0.0)
        exit_price = float(exits.get(leg_name, 0.0) or 0.0)
        if transaction_type == "SELL":
            pnl += (entry_price - exit_price) * qty
        else:
            pnl += (exit_price - entry_price) * qty
    return pnl


settings = load_settings()

# --- 1. LOAD SAVED STATES FIRST ---
def safe_expiry_date(index_symbol):
    expiry = config.get_next_expiry(index_symbol)
    try:
        return datetime.datetime.strptime(expiry, "%Y-%m-%d").date()
    except Exception:
        return datetime.datetime.now().date()


saved_nifty = safe_expiry_date("NIFTY")
saved_sensex = safe_expiry_date("SENSEX")

# --- THE UNIFIED BRAIN FIX: Read Expiries from settings.json ---
if "NIFTY_EXPIRY" in settings:
    try:
        saved_nifty = datetime.datetime.strptime(settings["NIFTY_EXPIRY"], "%Y-%m-%d").date()
    except Exception: pass

if "SENSEX_EXPIRY" in settings:
    try:
        saved_sensex = datetime.datetime.strptime(settings["SENSEX_EXPIRY"], "%Y-%m-%d").date()
    except Exception: pass

btst_state = False
if os.path.exists(BTST_FILE):
    try:
        with open(BTST_FILE, "r") as f:
            btst_state = (f.read().strip() == "TRUE")
    except Exception:
        pass

# --- SIDEBAR: AUTH & SETTINGS ---
st.sidebar.header("Algo Command Center")

api_key = settings.get("API_KEY", "")
api_secret = settings.get("API_SECRET", "")
redirect_uri = settings.get("REDIRECT_URI", "https://127.0.0.1:5000/")

st.sidebar.subheader("🟢 Live Authentication")
if not api_key or not api_secret:
    st.sidebar.error("⚠️ API_KEY and API_SECRET missing in settings.json")
else:
    with st.sidebar.expander("🔑 Generate Daily Live Token", expanded=False):
        auth_url = f"https://api.upstox.com/v2/login/authorization/dialog?response_type=code&client_id={api_key}&redirect_uri={urllib.parse.quote(redirect_uri)}"
        st.markdown(f"**Step 1:** [Click here to Login]({auth_url})")
        
        auth_code = st.text_input("Step 2: Paste the 'code' from URL")
        
        if st.button("Generate & Save Live Token"):
            if not auth_code:
                st.error("Please paste the auth code.")
            else:
                url = 'https://api.upstox.com/v2/login/authorization/token'
                headers = {'accept': 'application/json', 'Api-Version': '2.0', 'Content-Type': 'application/x-www-form-urlencoded'}
                data = {'code': auth_code, 'client_id': api_key, 'client_secret': api_secret, 'redirect_uri': redirect_uri, 'grant_type': 'authorization_code'}
                
                try:
                    response = requests.post(url, headers=headers, data=data)
                    response_data = response.json()
                    if 'access_token' in response_data:
                        settings['LIVE_ACCESS_TOKEN'] = response_data['access_token']
                        save_settings(settings)
                        st.success("✅ Live Token Saved!")
                    else:
                        st.error(f"Failed: {response_data}")
                except Exception as e:
                    st.error(f"Error: {e}")

# --- SIDEBAR: UNIFIED TRADING PARAMETERS ---
st.sidebar.subheader("Environment & Risk")

nifty_exp = st.sidebar.date_input("NIFTY Expiry Date", saved_nifty)
sensex_exp = st.sidebar.date_input("SENSEX Expiry Date", saved_sensex)

enable_btst = st.sidebar.toggle("🌙 Enable BTST (Carry Forward)", value=btst_state)
if enable_btst != btst_state:
    atomic_write_text(BTST_FILE, "TRUE" if enable_btst else "FALSE")

with st.sidebar.form("config_form"):
    env_mode = st.selectbox("Environment", ["SANDBOX", "LIVE"], index=0 if settings.get("ENVIRONMENT") == "SANDBOX" else 1)
    virtual_capital = float(settings.get("VIRTUAL_CAPITAL", config.VIRTUAL_CAPITAL))
    if env_mode == "SANDBOX":
        virtual_capital = st.number_input("Virtual Capital Allocation", value=virtual_capital, step=10000.0, min_value=0.0)
    else:
        st.caption("Live mode uses broker funds for sizing.")
    max_capital_utilization = st.number_input("Max Capital Utilization", value=float(settings.get("MAX_CAPITAL_UTILIZATION", config.MAX_CAPITAL_UTILIZATION)), step=0.01, min_value=0.10, max_value=1.00)
    st.caption(f"Dynamic sizing replaces manual quantities. Lot multiples: NIFTY {config.NIFTY_LOT_MULTIPLE}, SENSEX {config.SENSEX_LOT_MULTIPLE}.")
    buy_leg_percent = st.number_input("Buy Leg Premium %", value=float(settings.get("BUY_LEG_PERCENT", config.BUY_LEG_PERCENT)), step=0.5, min_value=1.0, max_value=20.0)
    vix_toggle_level = st.number_input("India VIX Toggle", value=float(settings.get("INDIA_VIX_TOGGLE_LEVEL", config.INDIA_VIX_TOGGLE_LEVEL)), step=0.5, min_value=5.0, max_value=40.0)
    targets_enabled = st.toggle("Enable Profit Target Exits", value=bool(settings.get("SNIPER_TARGETS_ENABLED", config.SNIPER_TARGETS_ENABLED)))
    btst_momentum_enabled = st.toggle("Enable BTST Momentum", value=bool(settings.get("BTST_MOMENTUM_ENABLED", config.BTST_MOMENTUM_ENABLED)))
    st.markdown("**Iron Condor**")
    iron_condor_drift_points = st.number_input("IC ATM Drift Points", value=float(settings.get("IRON_CONDOR_ATM_DRIFT_POINTS", config.IRON_CONDOR_ATM_DRIFT_POINTS)), step=5.0, min_value=1.0)
    iron_condor_target = st.number_input("IC Sniper Target %", value=float(settings.get("IRON_CONDOR_TARGET_PCT", config.IRON_CONDOR_TARGET_PCT)), step=1.0, min_value=1.0, max_value=100.0)
    iron_condor_kill = st.number_input("IC Catastrophe Kill x", value=float(settings.get("IRON_CONDOR_CATASTROPHE_MULTIPLIER", config.IRON_CONDOR_CATASTROPHE_MULTIPLIER)), step=0.05, min_value=1.0, max_value=3.0)
    st.markdown("**Iron Butterfly**")
    iron_butterfly_drift_points = st.number_input("IB ATM Drift Points", value=float(settings.get("IRON_BUTTERFLY_ATM_DRIFT_POINTS", config.IRON_BUTTERFLY_ATM_DRIFT_POINTS)), step=5.0, min_value=1.0)
    iron_butterfly_target = st.number_input("IB Sniper Target %", value=float(settings.get("IRON_BUTTERFLY_TARGET_PCT", config.IRON_BUTTERFLY_TARGET_PCT)), step=1.0, min_value=1.0, max_value=100.0)
    iron_butterfly_kill = st.number_input("IB Catastrophe Kill x", value=float(settings.get("IRON_BUTTERFLY_CATASTROPHE_MULTIPLIER", config.IRON_BUTTERFLY_CATASTROPHE_MULTIPLIER)), step=0.05, min_value=1.0, max_value=3.0)
    st.markdown("**Directional Spreads**")
    directional_target = st.number_input("Directional Target %", value=float(settings.get("DIRECTIONAL_TARGET_PCT", config.DIRECTIONAL_TARGET_PCT)), step=1.0, min_value=1.0, max_value=100.0)
    directional_kill = st.number_input("Directional Kill x", value=float(settings.get("DIRECTIONAL_CATASTROPHE_MULTIPLIER", config.DIRECTIONAL_CATASTROPHE_MULTIPLIER)), step=0.05, min_value=0.25, max_value=3.0)
    directional_btst_roc = st.number_input("BTST Auto-Exit ROC %", value=float(settings.get("DIRECTIONAL_BTST_AUTO_EXIT_ROC_PCT", config.DIRECTIONAL_BTST_AUTO_EXIT_ROC_PCT)), step=0.25, min_value=0.0, max_value=20.0)
    
    if st.form_submit_button("💾 Save Settings"):
        settings["ENVIRONMENT"] = env_mode
        settings["VIRTUAL_CAPITAL"] = virtual_capital
        settings["MAX_CAPITAL_UTILIZATION"] = max_capital_utilization
        settings["BUY_LEG_PERCENT"] = buy_leg_percent
        settings["INDIA_VIX_TOGGLE_LEVEL"] = vix_toggle_level
        settings["SNIPER_TARGETS_ENABLED"] = targets_enabled
        settings["BTST_MOMENTUM_ENABLED"] = btst_momentum_enabled
        settings["IRON_CONDOR_ATM_DRIFT_POINTS"] = iron_condor_drift_points
        settings["IRON_CONDOR_TARGET_PCT"] = iron_condor_target
        settings["IRON_CONDOR_CATASTROPHE_MULTIPLIER"] = iron_condor_kill
        settings["IRON_BUTTERFLY_ATM_DRIFT_POINTS"] = iron_butterfly_drift_points
        settings["IRON_BUTTERFLY_TARGET_PCT"] = iron_butterfly_target
        settings["IRON_BUTTERFLY_CATASTROPHE_MULTIPLIER"] = iron_butterfly_kill
        settings["DIRECTIONAL_TARGET_PCT"] = directional_target
        settings["DIRECTIONAL_CATASTROPHE_MULTIPLIER"] = directional_kill
        settings["DIRECTIONAL_BTST_AUTO_EXIT_ROC_PCT"] = directional_btst_roc
        
        # --- THE UNIFIED BRAIN FIX: Save Expiries to settings.json ---
        settings["NIFTY_EXPIRY"] = str(nifty_exp)
        settings["SENSEX_EXPIRY"] = str(sensex_exp)
        
        save_settings(settings)
        st.sidebar.success("Settings Saved!")

# --- SIDEBAR: ENGINE CONTROL ---
st.sidebar.markdown("---")
st.sidebar.header("🚀 Engine Control")

col_start, col_stop, col_force = st.sidebar.columns(3)

with col_start:
    if st.button("▶️ Start"):
        if not settings.get("LIVE_ACCESS_TOKEN"):
            st.error("Missing Live Token!")
        else:
            is_running = False
            old_pid = read_pid()
            if old_pid and psutil.pid_exists(old_pid):
                is_running = True

            if is_running:
                st.warning("Engine is already running!")
            else:
                remove_flag(GRACEFUL_STOP_FILE)
                remove_flag(MANUAL_EXIT_FILE)
                remove_flag(MANUAL_ENTRY_FILE)
                log_file = open(LOG_FILE_PATH, "a")
                process = subprocess.Popen(
                    [sys.executable, "main.py"], 
                    cwd=BASE_DIR, 
                    stdout=log_file, 
                    stderr=subprocess.STDOUT
                )
                atomic_write_text(PID_FILE, str(process.pid))
                st.success("Engine Started! Check logs.")

with col_stop:
    if st.button("⏹️ Stop"):
        atomic_write_text(GRACEFUL_STOP_FILE, "TRUE")
        st.warning("Graceful stop requested. Engine will stop after the current safety check.")

with col_force:
    if st.button("Kill"):
        pid = read_pid()
        if pid and psutil.pid_exists(pid):
            try:
                p = psutil.Process(pid)
                p.terminate()
                p.wait(timeout=10)
                st.success("Engine force-stopped. Check broker positions manually.")
            except Exception as e:
                st.error(f"Error force-stopping engine: {e}")
        else:
            st.warning("No running engine found.")
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)

st.sidebar.caption("Stop is graceful. Kill is emergency-only and requires a broker position check.")

# --- MAIN DASHBOARD ---
st.title("ALGO COMMAND CENTER")

live_ticks_header = {}
if os.path.exists(LIVE_FILE):
    try:
        with open(LIVE_FILE, "r") as lf:
            live_ticks_header = json.load(lf)
    except json.JSONDecodeError:
        live_ticks_header = {}

nifty_tick = live_ticks_header.get("NSE_INDEX|Nifty 50", {})
sensex_tick = live_ticks_header.get("BSE_INDEX|SENSEX", {})
vix_tick = live_ticks_header.get("NSE_INDEX|India VIX", {})
latest_state_header = {}
if os.path.exists(STATE_FILE):
    try:
        with open(STATE_FILE, "r") as sf:
            latest_state_header = json.load(sf)
    except json.JSONDecodeError:
        latest_state_header = {}
market_context_header = latest_state_header.get("market_context", {}) or latest_state_header.get("route_metadata", {}).get("market_context", {})
try:
    latest_telemetry = telemetry.get_latest_context()
except Exception:
    latest_telemetry = {}
cumulative_context = latest_telemetry.get("cumulative") or {}
if cumulative_context:
    market_context_header = cumulative_context


def tone_class(value):
    text = str(value or "").upper()
    if "BULL" in text or text == "EXPANDING":
        return "tone-bullish"
    if "BEAR" in text or text == "CONTRACTING":
        return "tone-bearish"
    if "NEUTRAL" in text or "FLAT" in text:
        return "tone-neutral"
    return "tone-muted"


def tile(label, value, tone="tone-muted", subtext=""):
    st.markdown(
        f"""
        <div class="market-tile">
            <div class="market-label">{label}</div>
            <div class="market-value {tone}">{value}</div>
            <div class="muted-header">{subtext}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

env_col, cap_col, data_col = st.columns([1, 1, 2])
with env_col:
    st.caption(f"mode: {settings.get('ENVIRONMENT', 'SANDBOX').lower()}")
with cap_col:
    if settings.get("ENVIRONMENT", "SANDBOX") == "SANDBOX":
        st.caption(f"virtual capital: ₹{float(settings.get('VIRTUAL_CAPITAL', config.VIRTUAL_CAPITAL)):,.0f}")
    else:
        st.caption("capital source: broker funds")
with data_col:
    st.caption(
        f"NIFTY: {nifty_tick.get('ltp', 'N/A')} | "
        f"SENSEX: {sensex_tick.get('ltp', 'N/A')} | "
        f"INDIA VIX: {vix_tick.get('ltp', 'N/A')} | "
        f"Flow: {market_context_header.get('flow_signal', 'N/A')} | "
        f"Straddle: {market_context_header.get('straddle_signal', 'N/A')}"
    )

price_col1, price_col2, price_col3 = st.columns(3)
with price_col1:
    tile("NIFTY", nifty_tick.get("ltp", "N/A"))
with price_col2:
    tile("SENSEX", sensex_tick.get("ltp", "N/A"))
with price_col3:
    tile("INDIA VIX", vix_tick.get("ltp", "N/A"))

flow_value = market_context_header.get("flow_signal", "N/A")
straddle_value = market_context_header.get("straddle_signal", "N/A")
regime_value = "NO TRADE"
if flow_value == "BULLISH" and straddle_value == "EXPANDING":
    regime_value = "BULL PUT SPREAD"
elif flow_value == "BEARISH" and straddle_value == "EXPANDING":
    regime_value = "BEAR CALL SPREAD"
elif flow_value == "NEUTRAL" and straddle_value == "CONTRACTING":
    regime_value = "IRON CONDOR RANGE"

signal_col1, signal_col2, signal_col3 = st.columns(3)
with signal_col1:
    tile("CUMULATIVE FLOW", flow_value, tone_class(flow_value), latest_telemetry.get("time", ""))
with signal_col2:
    tile("STRADDLE", straddle_value, tone_class(straddle_value), "vs session baseline")
with signal_col3:
    tile("REGIME STATUS", regime_value, tone_class(flow_value), "adaptive strategy")

# --- SYSTEM STATUS & MANUAL EXIT CONTROLS ---
col1, col2 = st.columns([3, 1])

with col1:
    engine_running = False
    pid = read_pid()
    hb_age, hb = heartbeat_age()
    if pid and psutil.pid_exists(pid) and hb_age is not None and hb_age <= 15:
        engine_running = True

    if engine_running:
        st.success(f"🟢 ENGINE STATUS: RUNNING | {hb.get('status', 'UNKNOWN')} | heartbeat {hb_age:.1f}s")
    elif pid and psutil.pid_exists(pid):
        stale_age = "N/A" if hb_age is None else f"{hb_age:.1f}s"
        st.warning(f"🟡 ENGINE PID EXISTS BUT HEARTBEAT STALE: {stale_age}")
    else:
        st.error("🔴 ENGINE STATUS: STOPPED")

is_trade_active = False
if os.path.exists(STATE_FILE):
    try:
        with open(STATE_FILE, "r") as f:
            is_trade_active = json.load(f).get("active", False)
    except Exception:
        pass

# Time Logic for Lockout
now = datetime.datetime.now()
market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
market_close = now.replace(hour=15, minute=25, second=0, microsecond=0)
fresh_entry_close = now.replace(hour=15, minute=10, second=0, microsecond=0)
is_trading_hours = (market_open <= now <= market_close) and (now.weekday() < 5)
is_fresh_entry_hours = (market_open <= now <= fresh_entry_close) and (now.weekday() < 5)

with col2:
    exit_locked = (not is_trade_active) or (not is_trading_hours)
    
    if st.button("🛑 MANUAL EXIT", type="primary", disabled=exit_locked):
        if os.path.exists(MANUAL_EXIT_FILE):
            st.toast("Manual exit is already requested. Waiting for engine confirmation.")
        else:
            atomic_write_text(MANUAL_EXIT_FILE, "TRUE")
            st.toast("Manual exit signal sent! Engine will square off immediately.")
        
    entry_locked = is_trade_active or (not is_fresh_entry_hours)
    if st.button("▶️ MANUAL ENTRY", type="secondary", disabled=entry_locked):
        atomic_write_text(MANUAL_ENTRY_FILE, "TRUE")
        st.toast("Manual entry signal sent! Engine will deploy a fresh trade.")
        
    if not is_trading_hours:
        st.caption("🔒 Locked: Outside Market Hours")

st.markdown("---")

col_status, col_logs = st.columns([1, 1.5])
            
with col_status:
    st.subheader("📡 Live System Status")
    
    if os.path.exists(STATE_FILE):
        state = {}
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
        except json.JSONDecodeError:
            pass 
            
        if state.get("active"):
            st.success(
                f"🟢 ACTIVE TRADE: {state['index_symbol']} | "
                f"Lots: {state.get('total_lots_deployed', 'N/A')} | "
                f"Qty: {state.get('total_quantity', state.get('quantity', 'N/A'))} | "
                f"Margin: ₹{float(state.get('margin_blocked', state.get('capital_deployed', 0.0)) or 0.0):,.0f}"
            )

            badge_col1, badge_col2, badge_col3 = st.columns(3)
            with badge_col1:
                st.metric("Sniper State", state.get("sniper_state", "INITIAL"), delta=f"Buy Leg: {config.BUY_LEG_PERCENT}%", delta_color="off")
            with badge_col2:
                live_net_state = state.get("live_net_premium", 0.0)
                st.metric("Live Net Premium", f"{live_net_state:.2f}", delta=f"Kill: {state.get('catastrophe_threshold', 0):.2f}", delta_color="off")
            with badge_col3:
                drift_ratio = state.get("atm_drift_ratio", 0.0)
                drift_points = state.get("atm_drift_points", 0.0)
                drift_points_threshold = state.get("atm_drift_points_threshold", 0.0)
                if drift_points_threshold:
                    drift_color = "normal" if drift_points < drift_points_threshold else "inverse"
                    st.metric("ATM Drift", f"{drift_points:.2f} pts", delta=f"Eject: {drift_points_threshold:.2f} pts", delta_color=drift_color)
                else:
                    drift_color = "normal" if drift_ratio < config.ATM_DRIFT_EJECT_THRESHOLD else "inverse"
                    st.metric("ATM Drift", f"{drift_ratio:.2f}x", delta=f"Eject: {config.ATM_DRIFT_EJECT_THRESHOLD:.2f}x", delta_color=drift_color)

            st.markdown("")

            live_ticks = {}
            if os.path.exists(LIVE_FILE):
                try:
                    with open(LIVE_FILE, "r") as lf:
                        live_ticks = json.load(lf)
                except json.JSONDecodeError:
                    pass 

            latest_tick_ts = max((tick.get("ts", 0) for tick in live_ticks.values() if isinstance(tick, dict)), default=0)
            feed_age = time.time() - latest_tick_ts if latest_tick_ts else None
            if feed_age is None:
                st.warning("Live feed: no tick timestamp available yet.")
            elif feed_age > settings.get("MAX_FEED_STALENESS_SECONDS", 5):
                st.error(f"Live feed stale: {feed_age:.1f}s old")
            else:
                st.caption(f"Live feed age: {feed_age:.1f}s")
                
            legs = state['legs']
            entries = state['entry_prices']
            strikes_data = state.get('strikes', {})
            sequence = order_sequence_for_state(state)
            sequence_map = {leg_name: tx for leg_name, tx in sequence}
            live_leg_prices = {
                leg_name: float(live_ticks.get(token, {}).get('ltp', entries.get(leg_name, 0.0)) or entries.get(leg_name, 0.0))
                for leg_name, token in legs.items()
            }
            
            st.markdown("### Live Position Tracker")
            table_data = []
            for leg_name, token in legs.items():
                tx = sequence_map.get(leg_name, "SELL" if leg_name.startswith("sell") else "BUY")
                entry_price = float(entries.get(leg_name, 0.0) or 0.0)
                live_price = float(live_leg_prices.get(leg_name, entry_price) or entry_price)
                pnl_point = (entry_price - live_price) if tx == "SELL" else (live_price - entry_price)
                table_data.append({
                    "Leg Type": f"{tx} {leg_name.upper()}",
                    "Strike Price": strikes_data.get(leg_name, 'N/A'),
                    "Trade Price": f"₹{entry_price:.2f}",
                    "Current Price": f"₹{live_price:.2f}",
                    "PnL / Point": pnl_point,
                })

            df_live = pd.DataFrame(table_data)
            
            def color_pnl(val):
                color = '#00ff00' if val > 0 else '#ff0000' if val < 0 else 'white'
                return f'color: {color}'
            
            st.dataframe(df_live.style.applymap(color_pnl, subset=['PnL / Point']), hide_index=True, width='stretch')
            
            # --- REAL PNL & TARGET CALCULATOR ---
            qty = state.get('quantity', config.NIFTY_LOT_MULTIPLE if state['index_symbol'] == 'NIFTY' else config.SENSEX_LOT_MULTIPLE)
            entry_net = net_from_sequence(entries, sequence)
            live_net = net_from_sequence(live_leg_prices, sequence)
            gross_pnl = pnl_from_sequence(entries, live_leg_prices, qty, sequence)
            
            sniper_target_pct = state.get("sniper_target_pct", config.SNIPER_TARGET_PCT)
            max_profit_rupees = float(state.get("max_profit_rupees", entry_net * qty) or 0.0)
            sniper_target_pnl = max_profit_rupees * (sniper_target_pct / 100.0)
            
            st.markdown("---")
            
            # --- PLOTLY GAUGE CHART ---
            if go:
                max_gauge = sniper_target_pnl * 1.4 if sniper_target_pnl > 0 else 5000
                min_gauge = -sniper_target_pnl if sniper_target_pnl > 0 else -5000
                
                fig = go.Figure(go.Indicator(
                    mode = "gauge+number+delta",
                    value = gross_pnl,
                    domain = {'x': [0, 1], 'y': [0, 1]},
                    title = {'text': "Real-time PnL", 'font': {'size': 20, 'color': 'white'}},
                    delta = {'reference': sniper_target_pnl, 'increasing': {'color': "#10b981"}, 'decreasing': {'color': "#ef4444"}},
                    gauge = {
                        'axis': {'range': [min_gauge, max_gauge], 'tickwidth': 1, 'tickcolor': "white"},
                        'bar': {'color': "#3b82f6"},
                        'bgcolor': "rgba(0,0,0,0)",
                        'borderwidth': 2,
                        'bordercolor': "gray",
                        'steps': [
                            {'range': [min_gauge, 0], 'color': "rgba(239, 68, 68, 0.2)"},
                            {'range': [0, sniper_target_pnl], 'color': "rgba(16, 185, 129, 0.2)"}
                        ],
                        'threshold': {
                            'line': {'color': "#10b981", 'width': 4},
                            'thickness': 0.75,
                            'value': sniper_target_pnl
                        }
                    }
                ))
                fig.update_layout(height=250, margin=dict(l=20, r=20, t=30, b=20), paper_bgcolor="rgba(0,0,0,0)", font={'color': "white", 'family': "Inter"})
                st.plotly_chart(fig, use_container_width=True)
                st.markdown("---")
            
            # --- 4-COLUMN LAYOUT FOR SNIPER METRICS ---
            metric_col1, metric_col2, metric_col3 = st.columns([1.3, 1.3, 1.2])
            
            with metric_col1:
                if gross_pnl >= 0:
                    st.metric("Live Gross PnL", f"₹{gross_pnl:.2f}", delta="In Profit", delta_color="normal")
                else:
                    st.metric("Live Gross PnL", f"-₹{abs(gross_pnl):.2f}", delta="In Loss", delta_color="inverse")
            
            with metric_col2:
                target_status = "ON" if state.get("sniper_targets_enabled", config.SNIPER_TARGETS_ENABLED) else "OFF"
                st.metric(f"Sniper Target ({target_status})", f"₹{sniper_target_pnl:.2f}", delta=f"{sniper_target_pct:.1f}%", delta_color="off")
            
            with metric_col3:
                catastrophe_multiplier = float(state.get("strategy_params", {}).get("catastrophe_multiplier", config.SNIPER_CATASTROPHE_MULTIPLIER))
                st.metric("Catastrophe Kill", f"{catastrophe_multiplier:.2f}x", delta=f"Net {state.get('catastrophe_threshold', 0):.2f}", delta_color="off")

        else:
            st.warning("🟡 SYSTEM IDLE: Waiting for schedule.")
    else:
        st.warning("🟡 SYSTEM IDLE: Waiting for schedule.")

with col_logs:
    st.subheader("🖥️ Live Engine Logs")
    if os.path.exists(LOG_FILE_PATH):
        with open(LOG_FILE_PATH, "r", encoding="utf-8", errors="replace") as file:
            lines = file.readlines()
            last_lines = lines[-15:] if len(lines) > 15 else lines
            log_text = "".join(last_lines)
            st.code(log_text, language="text")
    else:
        st.code("No engine logs found. Start the engine.", language="text")

st.markdown("---")

# --- 4. TRADE LEDGER & PNL ---
st.subheader("📜 Trade Ledger & Performance")

if os.path.exists(CSV_LOG_FILE):
    df = pd.read_csv(CSV_LOG_FILE)
    required_ledger_fields = [
        "Index_Name",
        "Strategy_Type",
        "Broker_Lot_Size",
        "Total_Lots_Deployed",
        "Total_Quantity",
        "Margin_Blocked",
    ]
    for field in required_ledger_fields:
        if field not in df.columns:
            df[field] = ""
    if "Index" in df.columns:
        df["Index_Name"] = df["Index_Name"].fillna("")
        df["Index_Name"] = df["Index_Name"].where(df["Index_Name"].astype(str).str.len() > 0, df["Index"])
    def highlight_exits(row):
        return ['background-color: #2b2b2b' if row['Action'] == 'EXIT' else '' for _ in row]
    st.dataframe(df.style.apply(highlight_exits, axis=1), width='stretch')

    if "PnL" in df.columns:
        total_pnl = df['PnL'].sum()
        color = "normal" if total_pnl >= 0 else "inverse"
        st.metric(label="Total Realized PnL", value=f"₹ {total_pnl:.2f}", delta=f"₹ {total_pnl:.2f}", delta_color=color)
else:
    st.info("No trades logged yet.")

st.markdown("---")
# --- AUTO-REFRESH TOGGLE ---
auto_refresh = st.toggle("🔄 Auto Refresh Dashboard (3s)", value=True, help="Disable to interact with inputs without being interrupted.")
if auto_refresh:
    time.sleep(3)
    st.rerun()
