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
import uuid

try:
    import plotly.graph_objects as go
except ImportError:
    go = None

st.set_page_config(page_title="Iron Butterfly V4", layout="wide")

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
st.sidebar.header("⚙️ Bot Configuration")

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
st.sidebar.subheader("📊 Trading Parameters")

nifty_exp = st.sidebar.date_input("NIFTY Expiry Date", saved_nifty)
sensex_exp = st.sidebar.date_input("SENSEX Expiry Date", saved_sensex)

enable_btst = st.sidebar.toggle("🌙 Enable BTST (Carry Forward)", value=btst_state)
if enable_btst != btst_state:
    atomic_write_text(BTST_FILE, "TRUE" if enable_btst else "FALSE")

with st.sidebar.form("config_form"):
    env_mode = st.selectbox("Environment", ["SANDBOX", "LIVE"], index=0 if settings.get("ENVIRONMENT") == "SANDBOX" else 1)
    nifty_qty = st.number_input("Nifty Qty (Multiples of 65)", value=settings.get("NIFTY_LOT_SIZE", 65), step=65)
    sensex_qty = st.number_input("Sensex Qty (Multiples of 20)", value=settings.get("SENSEX_LOT_SIZE", 20), step=20)
    sniper_wing_delta = st.number_input("Sniper Wing Delta", value=float(settings.get("SNIPER_WING_DELTA", config.SNIPER_WING_DELTA)), step=1.0, min_value=1.0, max_value=20.0)
    
    if st.form_submit_button("💾 Save Settings"):
        settings["ENVIRONMENT"] = env_mode
        settings["NIFTY_LOT_SIZE"] = nifty_qty
        settings["SENSEX_LOT_SIZE"] = sensex_qty
        settings["SNIPER_WING_DELTA"] = sniper_wing_delta
        
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
st.title("🦅 Iron Butterfly Command Center V4")

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
market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
is_trading_hours = (market_open <= now <= market_close) and (now.weekday() < 5)

with col2:
    exit_locked = (not is_trade_active) or (not is_trading_hours)
    
    if st.button("🛑 MANUAL EXIT", type="primary", disabled=exit_locked):
        if os.path.exists(MANUAL_EXIT_FILE):
            st.toast("Manual exit is already requested. Waiting for engine confirmation.")
        else:
            atomic_write_text(MANUAL_EXIT_FILE, "TRUE")
            st.toast("Manual exit signal sent! Engine will square off immediately.")
        
    entry_locked = is_trade_active or (not is_trading_hours)
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
            st.success(f"🟢 ACTIVE TRADE: {state['index_symbol']} | Qty: {state.get('quantity', 'N/A')}")

            badge_col1, badge_col2, badge_col3 = st.columns(3)
            with badge_col1:
                st.metric("Sniper State", state.get("sniper_state", "INITIAL"), delta=f"Wing Δ: {config.SNIPER_WING_DELTA}", delta_color="off")
            with badge_col2:
                live_net_state = state.get("live_net_premium", 0.0)
                st.metric("Live Net Premium", f"{live_net_state:.2f}", delta=f"Kill: {state.get('catastrophe_threshold', 0):.2f}", delta_color="off")
            with badge_col3:
                drift_ratio = state.get("atm_drift_ratio", 0.0)
                drift_color = "normal" if drift_ratio < config.SNIPER_DRIFT_EJECT_RATIO else "inverse"
                st.metric("ATM Drift", f"{drift_ratio:.2f}x", delta=f"Eject: {config.SNIPER_DRIFT_EJECT_RATIO:.2f}x", delta_color=drift_color)

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
            
            live_sell_pe = live_ticks.get(legs['sell_pe'], {}).get('ltp', entries['sell_pe'])
            live_sell_ce = live_ticks.get(legs['sell_ce'], {}).get('ltp', entries['sell_ce'])
            live_buy_pe = live_ticks.get(legs['buy_pe'], {}).get('ltp', entries['buy_pe'])
            live_buy_ce = live_ticks.get(legs['buy_ce'], {}).get('ltp', entries['buy_ce'])
            
            strikes_data = state.get('strikes', {})
            
            st.markdown("### Live Position Tracker")
            table_data = {
                "Leg Type": ["🔴 SELL PE", "🔴 SELL CE", "🟢 BUY PE", "🟢 BUY CE"],
                "Strike Price": [
                    strikes_data.get('sell_pe', 'N/A'),
                    strikes_data.get('sell_ce', 'N/A'),
                    strikes_data.get('buy_pe', 'N/A'),
                    strikes_data.get('buy_ce', 'N/A')
                ],
                "Trade Price": [f"₹{entries['sell_pe']:.2f}", f"₹{entries['sell_ce']:.2f}", f"₹{entries['buy_pe']:.2f}", f"₹{entries['buy_ce']:.2f}"],
                "Current Price": [f"₹{live_sell_pe:.2f}", f"₹{live_sell_ce:.2f}", f"₹{live_buy_pe:.2f}", f"₹{live_buy_ce:.2f}"],
                "PnL / Point": [
                    (entries['sell_pe'] - live_sell_pe), 
                    (entries['sell_ce'] - live_sell_ce), 
                    (live_buy_pe - entries['buy_pe']),   
                    (live_buy_ce - entries['buy_ce'])    
                ]
            }

            df_live = pd.DataFrame(table_data)
            
            def color_pnl(val):
                color = '#00ff00' if val > 0 else '#ff0000' if val < 0 else 'white'
                return f'color: {color}'
            
            st.dataframe(df_live.style.applymap(color_pnl, subset=['PnL / Point']), hide_index=True, width='stretch')
            
            # --- REAL PNL & TARGET CALCULATOR ---
            qty = state.get('quantity', settings.get("NIFTY_LOT_SIZE", 65) if state['index_symbol'] == 'NIFTY' else settings.get("SENSEX_LOT_SIZE", 20))
            
            entry_net = (entries['sell_ce'] + entries['sell_pe']) - (entries['buy_ce'] + entries['buy_pe'])
            live_net = (live_sell_ce + live_sell_pe) - (live_buy_ce + live_buy_pe)
            
            # --- GROSS PNL FIX APPLIED HERE ---
            gross_pnl = (entry_net - live_net) * qty
            
            sniper_target_pct = state.get("sniper_target_pct", config.SNIPER_TARGET_PCT)
            level_up_target_pct = state.get("level_up_target_pct", config.SNIPER_LEVEL_UP_TARGET_PCT)
            level_up_floor_pct = state.get("level_up_floor_pct", config.SNIPER_LEVEL_UP_FLOOR_PCT)
            sniper_target_pnl = (entry_net * (sniper_target_pct / 100.0)) * qty
            level_up_target_pnl = (entry_net * (level_up_target_pct / 100.0)) * qty
            level_up_floor_pnl = (entry_net * (level_up_floor_pct / 100.0)) * qty
            
            st.markdown("---")
            
            # --- PLOTLY GAUGE CHART ---
            if go:
                max_gauge = level_up_target_pnl * 1.4 if level_up_target_pnl > 0 else 5000
                min_gauge = -sniper_target_pnl if sniper_target_pnl > 0 else -5000
                
                fig = go.Figure(go.Indicator(
                    mode = "gauge+number+delta",
                    value = gross_pnl,
                    domain = {'x': [0, 1], 'y': [0, 1]},
                    title = {'text': "Real-time PnL", 'font': {'size': 20, 'color': 'white'}},
                    delta = {'reference': level_up_floor_pnl, 'increasing': {'color': "#10b981"}, 'decreasing': {'color': "#ef4444"}},
                    gauge = {
                        'axis': {'range': [min_gauge, max_gauge], 'tickwidth': 1, 'tickcolor': "white"},
                        'bar': {'color': "#3b82f6"},
                        'bgcolor': "rgba(0,0,0,0)",
                        'borderwidth': 2,
                        'bordercolor': "gray",
                        'steps': [
                            {'range': [min_gauge, 0], 'color': "rgba(239, 68, 68, 0.2)"},
                            {'range': [0, level_up_floor_pnl], 'color': "rgba(234, 179, 8, 0.2)"},
                            {'range': [level_up_floor_pnl, level_up_target_pnl], 'color': "rgba(16, 185, 129, 0.2)"}
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
            metric_col1, metric_col2, metric_col3, metric_col4 = st.columns([1.3, 1.3, 1.2, 1.2])
            
            with metric_col1:
                if gross_pnl >= 0:
                    st.metric("Live Gross PnL", f"₹{gross_pnl:.2f}", delta="In Profit", delta_color="normal")
                else:
                    st.metric("Live Gross PnL", f"-₹{abs(gross_pnl):.2f}", delta="In Loss", delta_color="inverse")
            
            with metric_col2:
                st.metric(f"Sniper Target ({sniper_target_pct:.1f}%)", f"₹{sniper_target_pnl:.2f}", delta=f"Pin <= {config.SNIPER_PINNED_DRIFT_RATIO:.2f}x", delta_color="off")
            
            with metric_col3:
                st.metric(f"Level Up Target ({level_up_target_pct:.1f}%)", f"₹{level_up_target_pnl:.2f}", delta=f"Floor ₹{level_up_floor_pnl:.2f}", delta_color="off")
                
            with metric_col4:
                st.metric("Catastrophe Kill", f"{config.SNIPER_CATASTROPHE_MULTIPLIER:.2f}x", delta=f"Net {state.get('catastrophe_threshold', 0):.2f}", delta_color="off")

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
