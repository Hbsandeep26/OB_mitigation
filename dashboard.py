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
CONSOLE_LOG_PATH = os.path.join(BASE_DIR, "console.log")
CSV_LOG_FILE = os.path.join(BASE_DIR, "sandbox_trade_logs.csv")
BTST_FILE = os.path.join(BASE_DIR, "btst_flag.txt")
PANIC_FILE = os.path.join(BASE_DIR, "panic_flag.txt")
LIVE_FILE = os.path.join(BASE_DIR, "live_prices.json")
HEARTBEAT_FILE = os.path.join(BASE_DIR, "engine_heartbeat.json")
GRACEFUL_STOP_FILE = os.path.join(BASE_DIR, "graceful_stop_flag.txt")
MANUAL_EXIT_FILE = os.path.join(BASE_DIR, "manual_exit_flag.txt")
MANUAL_ENTRY_FILE = os.path.join(BASE_DIR, "manual_entry_flag.txt")
CREDIT_SWEEP_STATE_FILE = os.path.join(BASE_DIR, "credit_sweep_state.json")

# --- STRATEGY SANDBOX PATHS & HELPMETHODS ---
FRAMEWORK_PID_FILE = os.path.join(BASE_DIR, "strategy_framework_pid.txt")
FRAMEWORK_CONSOLE_LOG_PATH = os.path.join(BASE_DIR, "strategy_framework_console.log")
STRATEGY_ENGINE_CONFIG_FILE = os.path.join(BASE_DIR, "strategy_engine_config.json")
FRAMEWORK_MASTER_LOG = os.path.join(BASE_DIR, "logs", "master_engine.log")
FRAMEWORK_LOG_DIR = os.path.join(BASE_DIR, "logs")
PERFORMANCE_DIR = os.path.join(BASE_DIR, "performance")

def read_pid_file(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return int(f.read().strip())
    except Exception:
        return None

def process_is_running(pid):
    if not pid:
        return False
    return psutil.pid_exists(pid)

def load_strategy_engine_config():
    if os.path.exists(STRATEGY_ENGINE_CONFIG_FILE):
        try:
            with open(STRATEGY_ENGINE_CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_strategy_engine_config(config_data):
    atomic_write_json(STRATEGY_ENGINE_CONFIG_FILE, config_data)

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


def net_from_sequence(prices, sequence, strategy_type=""):
    net = 0.0
    for leg_name, transaction_type in sequence:
        price = float(prices.get(leg_name, 0.0) or 0.0)
        mult = 1.0
        if strategy_type == "Ratio" and leg_name.startswith("sell_"):
            mult = 2.0
        net += mult * (price if transaction_type == "SELL" else -price)
    return net


def pnl_from_sequence(entries, exits, qty, sequence, strategy_type=""):
    pnl = 0.0
    for leg_name, transaction_type in sequence:
        entry_price = float(entries.get(leg_name, 0.0) or 0.0)
        exit_price = float(exits.get(leg_name, 0.0) or 0.0)
        mult = 1.0
        if strategy_type == "Ratio" and leg_name.startswith("sell_"):
            mult = 2.0
        leg_qty = qty * mult
        if transaction_type == "SELL":
            pnl += (entry_price - exit_price) * leg_qty
        else:
            pnl += (exit_price - entry_price) * leg_qty
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

st.sidebar.subheader("Broker Selection")
broker_options = ["DHAN", "UPSTOX"]
active_broker = str(settings.get("BROKER", "DHAN")).upper()
broker_index = broker_options.index(active_broker) if active_broker in broker_options else 0
broker_choice = st.sidebar.radio("Active Broker", broker_options, index=broker_index, horizontal=True)
if broker_choice != active_broker:
    settings["BROKER"] = broker_choice
    save_settings(settings)
    st.sidebar.success(f"Broker switched to {broker_choice}. Restart the engine if it is running.")

with st.sidebar.expander("Dhan Credentials", expanded=broker_choice == "DHAN"):
    dhan_client_id = st.text_input("DHAN_CLIENT_ID", value=settings.get("DHAN_CLIENT_ID", ""))
    dhan_access_token = st.text_input(
        "DHAN_ACCESS_TOKEN",
        value=settings.get("DHAN_ACCESS_TOKEN", ""),
        type="password",
    )
    dhan_pin = st.text_input(
        "DHAN_PIN",
        value=settings.get("DHAN_PIN", ""),
        type="password",
    )
    dhan_totp_secret = st.text_input(
        "DHAN_TOTP_SECRET",
        value=settings.get("DHAN_TOTP_SECRET", ""),
        type="password",
    )
    dhan_vix_id = st.text_input(
        "DHAN_INDIA_VIX_SECURITY_ID",
        value=settings.get("DHAN_INDIA_VIX_SECURITY_ID", ""),
        help="Optional. Required only if you want India VIX from Dhan.",
    )
    if st.button("Save Dhan Credentials"):
        settings["BROKER"] = "DHAN"
        settings["DHAN_CLIENT_ID"] = dhan_client_id.strip()
        settings["DHAN_ACCESS_TOKEN"] = dhan_access_token.strip()
        settings["DHAN_PIN"] = dhan_pin.strip()
        settings["DHAN_TOTP_SECRET"] = dhan_totp_secret.strip()
        settings["DHAN_INDIA_VIX_SECURITY_ID"] = dhan_vix_id.strip()
        save_settings(settings)
        st.success("Dhan credentials saved.")
        
    if st.button("🔑 Regenerate Dhan Token via TOTP"):
        if not dhan_client_id or not dhan_pin or not dhan_totp_secret:
            st.error("Please enter Client ID, PIN, and TOTP Secret first.")
        else:
            from auth import generate_dhan_token_with_totp
            token = generate_dhan_token_with_totp(dhan_client_id, dhan_pin, dhan_totp_secret)
            if token:
                settings["DHAN_ACCESS_TOKEN"] = token
                settings["DHAN_PIN"] = dhan_pin.strip()
                settings["DHAN_TOTP_SECRET"] = dhan_totp_secret.strip()
                save_settings(settings)
                st.success("Successfully generated & saved Dhan Access Token!")
                st.rerun()
            else:
                st.error("Failed to generate Dhan token. Check logs or credentials.")

api_key = settings.get("API_KEY", "")
api_secret = settings.get("API_SECRET", "")
redirect_uri = settings.get("REDIRECT_URI", "https://127.0.0.1:5000/")

st.sidebar.subheader("🟢 Live Authentication")
if broker_choice == "DHAN":
    if settings.get("DHAN_CLIENT_ID") and settings.get("DHAN_ACCESS_TOKEN"):
        st.sidebar.success("Dhan is configured as the active broker.")
    else:
        st.sidebar.warning("Add DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN before starting live Dhan mode.")
elif not api_key or not api_secret:
    st.sidebar.error("⚠️ API_KEY and API_SECRET missing in settings.json")
else:
    with st.sidebar.expander("🔑 Generate Daily Upstox Token", expanded=False):
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
    
    st.markdown("**MTF-OBLT Parameters**")
    
    mtf_strategy_type = st.selectbox("Strategy Type", ["Ratio", "Synthetic Future"], index=0 if settings.get("MTF_STRATEGY_TYPE", config.MTF_STRATEGY_TYPE) == "Ratio" else 1)
    mtf_trigger_type = st.selectbox("Trigger Type", ["choch"], index=0)
    mtf_stop_loss_type = st.selectbox("Stop Loss Type", ["5m_origin"], index=0)
    mtf_target_rr = st.number_input("Target RR", value=float(settings.get("MTF_TARGET_RR", config.MTF_TARGET_RR)), step=0.1, min_value=1.0)
    mtf_pivot_len = st.number_input("Pivot Length", value=int(settings.get("MTF_PIVOT_LEN", config.MTF_PIVOT_LEN)), step=1, min_value=1)
    mtf_disp_mult = st.number_input("Displacement Multiplier", value=float(settings.get("MTF_DISPLACEMENT_MULTIPLIER", config.MTF_DISPLACEMENT_MULTIPLIER)), step=0.1, min_value=0.5)
    mtf_inv_buf = st.number_input("Invalidation Buffer (ATR mult)", value=float(settings.get("MTF_INVALIDATION_BUFFER", config.MTF_INVALIDATION_BUFFER)), step=0.05, min_value=0.0)
    mtf_max_age = st.number_input("Max Leg Age (5m candles)", value=int(settings.get("MTF_MAX_LEG_AGE", config.MTF_MAX_LEG_AGE)), step=10, min_value=1)
    mtf_use_vwap = st.toggle("Use VWAP Filter", value=bool(settings.get("MTF_USE_VWAP_FILTER", config.MTF_USE_VWAP_FILTER)))
    mtf_max_trades = st.number_input("Max Trades per Day", value=int(settings.get("MTF_MAX_TRADES_PER_DAY", config.MTF_MAX_TRADES_PER_DAY)), step=1, min_value=1)
    mtf_discrete_risk = st.number_input("Discrete Risk Budget (₹)", value=float(settings.get("MTF_DISCRETE_RISK_BUDGET", config.MTF_DISCRETE_RISK_BUDGET)), step=100.0, min_value=0.0)
    
    mtf_symbols_list = settings.get("MTF_SCREENER_SYMBOLS", config.MTF_SCREENER_SYMBOLS)
    if isinstance(mtf_symbols_list, list):
        mtf_symbols_str = ", ".join(mtf_symbols_list)
    else:
        mtf_symbols_str = str(mtf_symbols_list)
        
    mtf_symbols_input = st.text_input("Screener Symbols (comma separated)", value=mtf_symbols_str)

    st.markdown("**Credit Sweep Strategy**")
    credit_sweep_enabled = st.toggle(
        "Enable Credit Sweep",
        value=bool(settings.get("CREDIT_SWEEP_ENABLED", config.CREDIT_SWEEP_ENABLED)),
    )
    credit_sweep_paper_only = st.toggle(
        "Paper Only Mode (Credit Sweep)",
        value=bool(settings.get("CREDIT_SWEEP_PAPER_ONLY", config.CREDIT_SWEEP_PAPER_ONLY)),
    )
    credit_symbols_list = settings.get("CREDIT_SWEEP_SYMBOLS", config.CREDIT_SWEEP_SYMBOLS)
    if isinstance(credit_symbols_list, list):
        credit_symbols_str = ", ".join(credit_symbols_list)
    else:
        credit_symbols_str = str(credit_symbols_list)
    credit_symbols_input = st.text_input("Credit Sweep Symbols", value=credit_symbols_str)
    credit_entry_start = st.text_input("Credit Sweep Entry Start", value=str(settings.get("CREDIT_SWEEP_ENTRY_START", config.CREDIT_SWEEP_ENTRY_START)))
    credit_entry_cutoff = st.text_input("Credit Sweep Entry Cutoff", value=str(settings.get("CREDIT_SWEEP_ENTRY_CUTOFF", config.CREDIT_SWEEP_ENTRY_CUTOFF)))
    credit_exit_time = st.text_input("Credit Sweep Exit Time", value=str(settings.get("CREDIT_SWEEP_EXIT_TIME", config.CREDIT_SWEEP_EXIT_TIME)))
    credit_min_score = st.number_input("Credit Sweep Min Score", value=int(settings.get("CREDIT_SWEEP_MIN_SCORE", config.CREDIT_SWEEP_MIN_SCORE)), step=1, min_value=0, max_value=100)
    credit_rr_target = st.number_input("Credit Sweep RR Target", value=float(settings.get("CREDIT_SWEEP_RR_TARGET", config.CREDIT_SWEEP_RR_TARGET)), step=0.05, min_value=0.1)
    credit_risk_budget = st.number_input("Credit Sweep Paper Risk Budget", value=float(settings.get("CREDIT_SWEEP_RISK_BUDGET", config.CREDIT_SWEEP_RISK_BUDGET)), step=50.0, min_value=0.0)
    
    if st.form_submit_button("💾 Save Settings"):
        settings["BROKER"] = broker_choice
        settings["ENVIRONMENT"] = env_mode
        settings["VIRTUAL_CAPITAL"] = virtual_capital
        
        settings["MTF_STRATEGY_TYPE"] = mtf_strategy_type
        settings["MTF_TRIGGER_TYPE"] = mtf_trigger_type
        settings["MTF_STOP_LOSS_TYPE"] = mtf_stop_loss_type
        settings["MTF_TARGET_RR"] = mtf_target_rr
        settings["MTF_PIVOT_LEN"] = mtf_pivot_len
        settings["MTF_DISPLACEMENT_MULTIPLIER"] = mtf_disp_mult
        settings["MTF_INVALIDATION_BUFFER"] = mtf_inv_buf
        settings["MTF_MAX_LEG_AGE"] = mtf_max_age
        settings["MTF_USE_VWAP_FILTER"] = mtf_use_vwap
        settings["MTF_MAX_TRADES_PER_DAY"] = mtf_max_trades
        settings["MTF_DISCRETE_RISK_BUDGET"] = mtf_discrete_risk
        
        parsed_symbols = [s.strip().upper() for s in mtf_symbols_input.split(",") if s.strip()]
        settings["MTF_SCREENER_SYMBOLS"] = parsed_symbols

        credit_symbols = [s.strip().upper() for s in credit_symbols_input.split(",") if s.strip()]
        settings["CREDIT_SWEEP_ENABLED"] = credit_sweep_enabled
        settings["CREDIT_SWEEP_PAPER_ONLY"] = credit_sweep_paper_only
        settings["CREDIT_SWEEP_SYMBOLS"] = credit_symbols
        settings["CREDIT_SWEEP_ENTRY_START"] = credit_entry_start
        settings["CREDIT_SWEEP_ENTRY_CUTOFF"] = credit_entry_cutoff
        settings["CREDIT_SWEEP_EXIT_TIME"] = credit_exit_time
        settings["CREDIT_SWEEP_MIN_SCORE"] = credit_min_score
        settings["CREDIT_SWEEP_RR_TARGET"] = credit_rr_target
        settings["CREDIT_SWEEP_RISK_BUDGET"] = credit_risk_budget
        
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
                with open(CONSOLE_LOG_PATH, "a", encoding="utf-8") as console_log:
                    process = subprocess.Popen(
                        [sys.executable, "main.py"], 
                        cwd=BASE_DIR, 
                        stdout=console_log, 
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

# --- STRATEGY SANDBOX SIDEBAR UI ---
st.sidebar.header("Strategy Sandbox")
framework_config = load_strategy_engine_config()
framework_pid = read_pid_file(FRAMEWORK_PID_FILE)
framework_running = process_is_running(framework_pid)
enabled_count = sum(1 for item in framework_config.get("strategies", []) if item.get("enabled", True))

if framework_running:
    st.sidebar.success(f"Sandbox running. PID {framework_pid}")
else:
    st.sidebar.info("Sandbox idle")

with st.sidebar.expander("Sandbox Strategy Selection", expanded=False):
    with st.form("strategy_sandbox_form"):
        for idx, item in enumerate(framework_config.get("strategies", [])):
            name = item.get("name", f"Strategy {idx}")
            item["enabled"] = st.checkbox(
                name,
                value=bool(item.get("enabled", True)),
                key=f"sandbox_strategy_enabled_{idx}",
            )
        replay_sleep = float((framework_config.get("data", {}) or {}).get("replay_sleep_seconds", 0.0))
        replay_sleep = st.number_input(
            "Replay Sleep Seconds",
            value=replay_sleep,
            min_value=0.0,
            step=0.05,
            help="0 runs the backtest-style replay as fast as possible.",
        )
        if st.form_submit_button("Save Sandbox Config"):
            framework_config.setdefault("data", {})["replay_sleep_seconds"] = replay_sleep
            save_strategy_engine_config(framework_config)
            st.success("Sandbox strategy config saved.")
            st.rerun()

    col_all, col_none = st.columns(2)
    with col_all:
        if st.button("Enable All", key="sandbox_enable_all"):
            for item in framework_config.get("strategies", []):
                item["enabled"] = True
            save_strategy_engine_config(framework_config)
            st.rerun()
    with col_none:
        if st.button("Disable All", key="sandbox_disable_all"):
            for item in framework_config.get("strategies", []):
                item["enabled"] = False
            save_strategy_engine_config(framework_config)
            st.rerun()

sandbox_start, sandbox_stop = st.sidebar.columns(2)
with sandbox_start:
    if st.button("Run Sandbox", disabled=framework_running or enabled_count == 0):
        if enabled_count == 0:
            st.error("Enable at least one sandbox strategy first.")
        else:
            with open(FRAMEWORK_CONSOLE_LOG_PATH, "a", encoding="utf-8") as console_log:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "main.py",
                        "--framework",
                        "--config",
                        STRATEGY_ENGINE_CONFIG_FILE,
                    ],
                    cwd=BASE_DIR,
                    stdout=console_log,
                    stderr=subprocess.STDOUT,
                )
            atomic_write_text(FRAMEWORK_PID_FILE, str(process.pid))
            st.success("Strategy sandbox started.")
            st.rerun()

with sandbox_stop:
    if st.button("Stop Sandbox", disabled=not framework_running):
        try:
            psutil.Process(framework_pid).terminate()
            st.warning("Strategy sandbox stop requested.")
        except Exception as e:
            st.error(f"Could not stop sandbox: {e}")
        if os.path.exists(FRAMEWORK_PID_FILE):
            os.remove(FRAMEWORK_PID_FILE)
        st.rerun()

# --- MAIN DASHBOARD ---
st.title("ALGO COMMAND CENTER")

tab_dashboard, tab_scanner, tab_sandbox = st.tabs(["📊 Performance Dashboard", "🔍 Market Scanner", "Strategy Sandbox"])

with tab_dashboard:


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
                    mult = 2.0 if (state.get("strategy_type") == "Ratio" and leg_name.startswith("sell_")) else 1.0
                    pnl_contrib = mult * ((entry_price - live_price) if tx == "SELL" else (live_price - entry_price))
                    table_data.append({
                        "Leg Type": f"{tx} {leg_name.upper()}",
                        "Multiplier": f"{int(mult)}x",
                        "Strike Price": strikes_data.get(leg_name, 'N/A'),
                        "Trade Price": f"₹{entry_price:.2f}",
                        "Current Price": f"₹{live_price:.2f}",
                        "PnL Contribution / Pt": pnl_contrib,
                    })

                df_live = pd.DataFrame(table_data)
            
                def color_pnl(val):
                    color = '#00ff00' if val > 0 else '#ff0000' if val < 0 else 'white'
                    return f'color: {color}'
            
                st.dataframe(df_live.style.applymap(color_pnl, subset=['PnL Contribution / Pt']), hide_index=True, width='stretch')
            
                # --- REAL PNL & TARGET CALCULATOR ---
                qty = state.get('quantity', config.NIFTY_LOT_MULTIPLE if state['index_symbol'] == 'NIFTY' else config.SENSEX_LOT_MULTIPLE)
                entry_net = net_from_sequence(entries, sequence, strategy_type=state.get("strategy_type", ""))
                live_net = net_from_sequence(live_leg_prices, sequence, strategy_type=state.get("strategy_type", ""))
                gross_pnl = pnl_from_sequence(entries, live_leg_prices, qty, sequence, strategy_type=state.get("strategy_type", ""))
            
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

        st.markdown("---")
        st.markdown("### Credit Sweep Paper")
        credit_state = {}
        if os.path.exists(CREDIT_SWEEP_STATE_FILE):
            try:
                with open(CREDIT_SWEEP_STATE_FILE, "r", encoding="utf-8") as cf:
                    credit_state = json.load(cf)
            except json.JSONDecodeError:
                credit_state = {}

        position = credit_state.get("position") or {}
        if credit_state.get("active") and position:
            st.success(
                f"PAPER ACTIVE: {position.get('symbol', '')} {position.get('direction', '')} | "
                f"{position.get('strategy_type', '')}"
            )
            cs_col1, cs_col2, cs_col3 = st.columns(3)
            with cs_col1:
                st.metric("Paper PnL", f"₹{float(position.get('paper_pnl', 0.0) or 0.0):.2f}", delta=f"{float(position.get('paper_rr', 0.0) or 0.0):.2f}R")
            with cs_col2:
                st.metric("Net Credit", f"{float(position.get('net_credit', 0.0) or 0.0):.2f}", delta=f"Loss {float(position.get('defined_loss', 0.0) or 0.0):.2f}", delta_color="off")
            with cs_col3:
                st.metric("Spot Path", f"{float(position.get('current_spot', position.get('entry_spot', 0.0)) or 0.0):.2f}", delta=f"T {float(position.get('target_price', 0.0) or 0.0):.2f} / S {float(position.get('stop_price', 0.0) or 0.0):.2f}", delta_color="off")
        elif position.get("exit_reason"):
            st.info(
                f"Last paper exit: {position.get('symbol', '')} {position.get('exit_reason', '')} | "
                f"PnL ₹{float(position.get('paper_pnl', 0.0) or 0.0):.2f}"
            )
        else:
            st.caption("No active Credit Sweep paper trade.")

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

        # --- 4b. DAILY & WEEKLY PERFORMANCE SUMMARIES ---
        try:
            import sandbox_analyzer
            daily_df = sandbox_analyzer.generate_daily_summary(df)
            weekly_df = sandbox_analyzer.generate_weekly_summary(df)
            
            if not daily_df.empty or not weekly_df.empty:
                st.markdown("#### 📅 Performance Reports")
                sum_tab1, sum_tab2 = st.tabs(["Daily Performance Summary", "Weekly Performance Summary"])
                
                with sum_tab1:
                    if not daily_df.empty:
                        def highlight_daily(row):
                            val = float(row['Net PnL (INR)'])
                            color = 'color: #00ff00' if val > 0 else ('color: #ff0000' if val < 0 else '')
                            return [color if col == 'Net PnL (INR)' else '' for col in row.index]
                        st.dataframe(daily_df.style.apply(highlight_daily, axis=1), hide_index=True, width='stretch')
                    else:
                        st.info("No daily summaries available yet.")
                        
                with sum_tab2:
                    if not weekly_df.empty:
                        def highlight_weekly(row):
                            val = float(row['Net PnL (INR)'])
                            color = 'color: #00ff00' if val > 0 else ('color: #ff0000' if val < 0 else '')
                            return [color if col == 'Net PnL (INR)' else '' for col in row.index]
                        st.dataframe(weekly_df.style.apply(highlight_weekly, axis=1), hide_index=True, width='stretch')
                    else:
                        st.info("No weekly summaries available yet.")
        except Exception as e:
            st.error(f"Error generating performance summaries: {e}")
    else:
        st.info("No trades logged yet.")

    st.markdown("---")
    # --- AUTO-REFRESH TOGGLE ---

with tab_scanner:
    st.subheader("🔍 Real-time Market Screener (5m OB + 1m Trigger)")
    screener_file = os.path.join(BASE_DIR, "screener_state.json")
    if os.path.exists(screener_file):
        try:
            with open(screener_file, "r") as sf:
                scan_data = json.load(sf)
            if scan_data:
                import pandas as pd
                df_scan = pd.DataFrame(scan_data)
                
                rename_dict = {
                    "symbol": "Symbol",
                    "price": "Price",
                    "trend": "Trend (5m)",
                    "zone_time": "Zone Time",
                    "zone_entry": "Zone Entry",
                    "stop_loss": "Stop Loss",
                    "target": "Target",
                    "pullback": "Pullback",
                    "trigger": "Trigger Status",
                    "score": "Score",
                    "live_rr": "Live RR",
                    "signal_age": "Age (s)",
                    "reject_reason": "Reject Reason",
                    "updated_at": "Updated At"
                }
                df_display = df_scan.rename(columns=rename_dict)
                
                def style_scanner(row):
                    color = ''
                    status = row.get('Trigger Status')
                    pullback = row.get('Pullback')
                    if status == 'CONFIRMED':
                        color = 'background-color: rgba(16, 185, 129, 0.2)'
                    elif status == 'REJECTED':
                        color = 'background-color: rgba(239, 68, 68, 0.15)'
                    elif pullback == 'PENDING':
                        color = 'background-color: rgba(59, 130, 246, 0.1)'
                    return [color] * len(row)
                st.dataframe(df_display.style.apply(style_scanner, axis=1), hide_index=True, width='stretch')
            else:
                st.info("No symbols currently scanned.")
        except Exception as e:
            st.error(f"Error loading screener state: {e}")
    else:
        st.info("Screener file not found. Make sure the trading engine is running and scanning.")

    st.markdown("---")
    st.subheader("Credit Sweep Paper Scanner")
    if os.path.exists(CREDIT_SWEEP_STATE_FILE):
        try:
            with open(CREDIT_SWEEP_STATE_FILE, "r", encoding="utf-8") as cf:
                credit_state = json.load(cf)
            credit_rows = credit_state.get("scanner", [])
            if credit_rows:
                df_credit = pd.DataFrame(credit_rows)
                credit_columns = [
                    "symbol",
                    "status",
                    "direction",
                    "score",
                    "entry_price",
                    "stop_price",
                    "target_price",
                    "rr_target",
                    "fresh_spot",
                    "net_credit",
                    "defined_loss",
                    "reject_reason",
                    "updated_at",
                ]
                for col in credit_columns:
                    if col not in df_credit.columns:
                        df_credit[col] = ""
                df_credit = df_credit[credit_columns].rename(columns={
                    "symbol": "Symbol",
                    "status": "Status",
                    "direction": "Direction",
                    "score": "Score",
                    "entry_price": "Entry",
                    "stop_price": "Stop",
                    "target_price": "Target",
                    "rr_target": "R:R",
                    "fresh_spot": "Fresh Spot",
                    "net_credit": "Net Credit",
                    "defined_loss": "Defined Loss",
                    "reject_reason": "Reject Reason",
                    "updated_at": "Updated At",
                })

                def style_credit(row):
                    status = row.get("Status")
                    if status == "PAPER_ENTRY":
                        color = "background-color: rgba(16, 185, 129, 0.22)"
                    elif status == "CONFIRMED":
                        color = "background-color: rgba(59, 130, 246, 0.14)"
                    elif status == "REJECTED":
                        color = "background-color: rgba(239, 68, 68, 0.14)"
                    else:
                        color = ""
                    return [color] * len(row)

                st.dataframe(df_credit.style.apply(style_credit, axis=1), hide_index=True, width='stretch')
            else:
                st.info("Credit Sweep has not scanned yet.")
        except Exception as e:
            st.error(f"Error loading Credit Sweep state: {e}")
    else:
        st.info("Credit Sweep state file not found yet.")

with tab_sandbox:
    st.subheader("Strategy Sandbox Runner")
    sandbox_pid = read_pid_file(FRAMEWORK_PID_FILE)
    sandbox_running = process_is_running(sandbox_pid)
    sandbox_config = load_strategy_engine_config()
    strategies = sandbox_config.get("strategies", [])

    status_col, enabled_col, mode_col = st.columns(3)
    with status_col:
        st.metric("Status", "RUNNING" if sandbox_running else "IDLE", delta=f"PID {sandbox_pid}" if sandbox_running else "")
    with enabled_col:
        st.metric("Enabled Strategies", str(sum(1 for item in strategies if item.get("enabled", True))))
    with mode_col:
        st.metric("Mode", str(sandbox_config.get("mode", "paper")).upper())

    st.markdown("### Configured Strategies")
    if strategies:
        strategy_rows = []
        for item in strategies:
            strategy_rows.append({
                "Name": item.get("name", ""),
                "Enabled": bool(item.get("enabled", True)),
                "Class": item.get("class", ""),
                "Symbols": ", ".join(item.get("symbols", [])),
                "Timeframes": ", ".join(item.get("timeframes", [])),
            })
        st.dataframe(pd.DataFrame(strategy_rows), hide_index=True, width="stretch")
    else:
        st.info("No strategies found in strategy_engine_config.json.")

    st.markdown("### Weekly Performance Summaries")
    summary_files = []
    if os.path.exists(PERFORMANCE_DIR):
        summary_files = sorted(
            [name for name in os.listdir(PERFORMANCE_DIR) if name.endswith("_summary.json")],
            reverse=True,
        )
    if summary_files:
        summary_rows = []
        for name in summary_files:
            try:
                with open(os.path.join(PERFORMANCE_DIR, name), "r", encoding="utf-8") as sf:
                    summary_rows.append(json.load(sf))
            except Exception:
                pass
        if summary_rows:
            st.dataframe(pd.DataFrame(summary_rows), hide_index=True, width="stretch")
        else:
            st.info("Summary files exist but could not be read.")
    else:
        st.info("No sandbox summaries yet. Run Sandbox once to generate them.")

    log_col1, log_col2 = st.columns(2)
    with log_col1:
        st.markdown("### Master Engine Log")
        if os.path.exists(FRAMEWORK_MASTER_LOG):
            with open(FRAMEWORK_MASTER_LOG, "r", encoding="utf-8", errors="replace") as mf:
                st.code("".join(mf.readlines()[-80:]), language="text")
        else:
            st.code("No master_engine.log yet.", language="text")

    with log_col2:
        st.markdown("### Strategy Log")
        log_files = []
        if os.path.exists(FRAMEWORK_LOG_DIR):
            log_files = sorted(name for name in os.listdir(FRAMEWORK_LOG_DIR) if name.startswith("strategy_") and name.endswith(".log"))
        if log_files:
            selected_log = st.selectbox("Select strategy log", log_files)
            with open(os.path.join(FRAMEWORK_LOG_DIR, selected_log), "r", encoding="utf-8", errors="replace") as lf:
                st.code("".join(lf.readlines()[-80:]), language="text")
        else:
            st.code("No strategy logs yet.", language="text")

    with st.expander("Sandbox Console Output", expanded=False):
        if os.path.exists(FRAMEWORK_CONSOLE_LOG_PATH):
            with open(FRAMEWORK_CONSOLE_LOG_PATH, "r", encoding="utf-8", errors="replace") as cf:
                st.code("".join(cf.readlines()[-120:]), language="text")
        else:
            st.code("No sandbox console output yet.", language="text")

auto_refresh = st.toggle("🔄 Auto Refresh Dashboard (3s)", value=True, help="Disable to interact with inputs without being interrupted.")
if auto_refresh:
    time.sleep(3)
    st.rerun()
