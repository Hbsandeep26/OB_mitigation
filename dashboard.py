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

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_settings(new_settings):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(new_settings, f, indent=4)

settings = load_settings()

# --- 1. LOAD SAVED STATES FIRST ---
saved_nifty = datetime.date.today()
saved_sensex = datetime.date.today()

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
with open(BTST_FILE, "w") as f:
    f.write("TRUE" if enable_btst else "FALSE")

with st.sidebar.form("config_form"):
    env_mode = st.selectbox("Environment", ["SANDBOX", "LIVE"], index=0 if settings.get("ENVIRONMENT") == "SANDBOX" else 1)
    nifty_qty = st.number_input("Nifty Qty (Multiples of 65)", value=settings.get("NIFTY_LOT_SIZE", 65), step=65)
    sensex_qty = st.number_input("Sensex Qty (Multiples of 20)", value=settings.get("SENSEX_LOT_SIZE", 20), step=20)
    
    if st.form_submit_button("💾 Save Settings"):
        settings["ENVIRONMENT"] = env_mode
        settings["NIFTY_LOT_SIZE"] = nifty_qty
        settings["SENSEX_LOT_SIZE"] = sensex_qty
        
        # --- THE UNIFIED BRAIN FIX: Save Expiries to settings.json ---
        settings["NIFTY_EXPIRY"] = str(nifty_exp)
        settings["SENSEX_EXPIRY"] = str(sensex_exp)
        
        save_settings(settings)
        st.sidebar.success("Settings Saved!")

# --- SIDEBAR: ENGINE CONTROL ---
st.sidebar.markdown("---")
st.sidebar.header("🚀 Engine Control")

col_start, col_stop = st.sidebar.columns(2)

with col_start:
    if st.button("▶️ Start"):
        if not settings.get("LIVE_ACCESS_TOKEN"):
            st.error("Missing Live Token!")
        else:
            is_running = False
            if os.path.exists(PID_FILE):
                with open(PID_FILE, "r") as f:
                    old_pid = int(f.read().strip())
                if psutil.pid_exists(old_pid):
                    is_running = True

            if is_running:
                st.warning("Engine is already running!")
            else:
                log_file = open(LOG_FILE_PATH, "a")
                process = subprocess.Popen(
                    [sys.executable, "main.py"], 
                    cwd=BASE_DIR, 
                    stdout=log_file, 
                    stderr=subprocess.STDOUT
                )
                with open(PID_FILE, "w") as f:
                    f.write(str(process.pid))
                st.success("Engine Started! Check logs.")

with col_stop:
    if st.button("⏹️ Stop"):
        if os.path.exists(PID_FILE):
            with open(PID_FILE, "r") as f:
                try:
                    pid = int(f.read().strip())
                    if psutil.pid_exists(pid):
                        p = psutil.Process(pid)
                        p.terminate() 
                        p.wait()
                        st.success("Engine Stopped!")
                    else:
                        st.warning("Engine was not running.")
                except Exception as e:
                    st.error(f"Error stopping engine: {e}")
            os.remove(PID_FILE)
        else:
            st.warning("No running engine found.")

st.sidebar.caption("You can now safely control the bot entirely from this UI.")

# --- MAIN DASHBOARD ---
st.title("🦅 Iron Butterfly Command Center V4")

# --- SYSTEM STATUS & MANUAL EXIT CONTROLS ---
col1, col2 = st.columns([3, 1])

with col1:
    # --- ENGINE STATUS: Cross-platform detection via PID file ---
    engine_running = False
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, "r") as f:
                pid = int(f.read().strip())
            if psutil.pid_exists(pid):
                engine_running = True
        except Exception:
            pass

    if engine_running:
        st.success("🟢 ENGINE STATUS: RUNNING")
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
    button_locked = (not is_trade_active) or (not is_trading_hours)
    
    if st.button("🛑 MANUAL EXIT", type="primary", disabled=button_locked):
        manual_file = os.path.join(BASE_DIR, "manual_exit_flag.txt")
        with open(manual_file, "w") as f:
            f.write("TRUE")
        st.toast("Manual exit signal sent! Engine will square off immediately.")
        
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

            # ================================================================
            # VIX PROFILE BADGES (NEW — reads from trade_state.json)
            # ================================================================
            vix_profile = state.get("vix_profile", "N/A")
            session_vix = state.get("session_vix", 0.0)
            
            profile_colors = {
                "LOW_VIX": "🟢",
                "MID_VIX": "🟡", 
                "HIGH_VIX": "🔴"
            }
            profile_emoji = profile_colors.get(vix_profile, "⚪")
            
            badge_col1, badge_col2, badge_col3 = st.columns(3)
            with badge_col1:
                st.metric("VIX Profile", f"{profile_emoji} {vix_profile}", delta=f"VIX: {session_vix:.1f}", delta_color="off")
            with badge_col2:
                hwm = state.get("profit_high_water_mark", 0.0) * 100
                trail_active = state.get("trail_active", False)
                trail_status = "🟢 ACTIVE" if trail_active else "⚪ Waiting"
                st.metric("Ratchet Trail", trail_status, delta=f"HWM: {hwm:.2f}%", delta_color="off")
            with badge_col3:
                drift_ratio = state.get("atm_drift_ratio", 0.0)
                drift_color = "normal" if drift_ratio < 1.0 else "inverse"
                st.metric("ATM Drift", f"{drift_ratio:.2f}x", delta=f"Limit: 1.5x", delta_color=drift_color)

            st.markdown("")

            live_ticks = {}
            if os.path.exists(LIVE_FILE):
                try:
                    with open(LIVE_FILE, "r") as lf:
                        live_ticks = json.load(lf)
                except json.JSONDecodeError:
                    pass 
                
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
            
            # ================================================================
            # TARGET MATH — READS FROM TRADE STATE (VIX-SYNCED!)
            # Falls back to settings.json if trade_state doesn't have it
            # ================================================================
            current_target_pct = state.get("applied_target_pct", settings.get("TARGET_PROFIT_PCT", 10))
            target_gross_pnl = (entry_net * (current_target_pct / 100.0)) * qty

            # Show trail floor if ratchet is active
            trail_floor_pct = state.get("trail_floor", 0.0) * 100
            trail_floor_pnl = (entry_net * state.get("trail_floor", 0.0)) * qty if state.get("trail_active") else 0.0
            
            st.markdown("---")
            
            # --- PLOTLY GAUGE CHART ---
            if go:
                max_gauge = target_gross_pnl * 1.5 if target_gross_pnl > 0 else 5000
                min_gauge = -target_gross_pnl if target_gross_pnl > 0 else -5000
                
                fig = go.Figure(go.Indicator(
                    mode = "gauge+number+delta",
                    value = gross_pnl,
                    domain = {'x': [0, 1], 'y': [0, 1]},
                    title = {'text': "Real-time PnL", 'font': {'size': 20, 'color': 'white'}},
                    delta = {'reference': trail_floor_pnl, 'increasing': {'color': "#10b981"}, 'decreasing': {'color': "#ef4444"}},
                    gauge = {
                        'axis': {'range': [min_gauge, max_gauge], 'tickwidth': 1, 'tickcolor': "white"},
                        'bar': {'color': "#3b82f6"},
                        'bgcolor': "rgba(0,0,0,0)",
                        'borderwidth': 2,
                        'bordercolor': "gray",
                        'steps': [
                            {'range': [min_gauge, 0], 'color': "rgba(239, 68, 68, 0.2)"},
                            {'range': [0, trail_floor_pnl], 'color': "rgba(234, 179, 8, 0.2)"},
                            {'range': [trail_floor_pnl, target_gross_pnl], 'color': "rgba(16, 185, 129, 0.2)"}
                        ],
                        'threshold': {
                            'line': {'color': "#10b981", 'width': 4},
                            'thickness': 0.75,
                            'value': target_gross_pnl
                        }
                    }
                ))
                fig.update_layout(height=250, margin=dict(l=20, r=20, t=30, b=20), paper_bgcolor="rgba(0,0,0,0)", font={'color': "white", 'family': "Inter"})
                st.plotly_chart(fig, use_container_width=True)
                st.markdown("---")
            
            # --- 4-COLUMN LAYOUT FOR METRICS AND HOT-SWAP ---
            metric_col1, metric_col2, metric_col3, metric_col4 = st.columns([1.3, 1.3, 1.2, 1.2])
            
            with metric_col1:
                if gross_pnl >= 0:
                    st.metric("Live Gross PnL", f"₹{gross_pnl:.2f}", delta="In Profit", delta_color="normal")
                else:
                    st.metric("Live Gross PnL", f"-₹{abs(gross_pnl):.2f}", delta="In Loss", delta_color="inverse")
            
            with metric_col2:
                target_label = f"🎯 Target ({current_target_pct}%)"
                if state.get("trail_active"):
                    target_label = f"🛡️ Trail Floor ({trail_floor_pct:.1f}%)"
                    target_gross_pnl = trail_floor_pnl
                st.metric(target_label, f"₹{target_gross_pnl:.2f}", delta=f"VIX: {vix_profile}", delta_color="off")
            
            with metric_col3:
                # Show what determined the target
                target_source = "VIX Auto" if "applied_target_pct" in state else "Manual"
                st.metric("Target Source", target_source, delta=f"{current_target_pct}%", delta_color="off")
                
            with metric_col4:
                new_target = st.number_input(
                    "Hot-Swap Target (%)", 
                    value=current_target_pct, 
                    step=1,
                    help="Override the VIX target. Changes apply instantly to the live trade."
                )
                
                # Save instantly and trigger UI refresh if changed
                if new_target != current_target_pct:
                    settings["TARGET_PROFIT_PCT"] = new_target
                    save_settings(settings)
                    
                    # Also update the trade_state so the engine picks it up
                    if os.path.exists(STATE_FILE):
                        try:
                            with open(STATE_FILE, "r") as f:
                                live_state = json.load(f)
                            live_state["applied_target_pct"] = new_target
                            with open(STATE_FILE, "w") as f:
                                json.dump(live_state, f, indent=4)
                        except Exception:
                            pass
                    
                    st.toast(f"Target overridden to {new_target}%! (Manual takes priority over VIX)")
                    st.rerun()

        else:
            st.warning("🟡 SYSTEM IDLE: Waiting for schedule.")
    else:
        st.warning("🟡 SYSTEM IDLE: Waiting for schedule.")

with col_logs:
    st.subheader("🖥️ Live Engine Logs")
    if os.path.exists(LOG_FILE_PATH):
        with open(LOG_FILE_PATH, "r") as file:
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
