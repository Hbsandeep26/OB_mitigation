import os

dashboard_path = "dashboard.py"

with open(dashboard_path, "r", encoding="utf-8") as f:
    content = f.read()

# Normalize line endings to LF
content = content.replace("\r\n", "\n")

# 1. Update net_from_sequence and pnl_from_sequence
old_funcs = """def net_from_sequence(prices, sequence):
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
    return pnl"""

new_funcs = """def net_from_sequence(prices, sequence, strategy_type=""):
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
    return pnl"""

if old_funcs in content:
    content = content.replace(old_funcs, new_funcs)
    print("SUCCESS: Updated net_from_sequence and pnl_from_sequence.")
else:
    print("WARNING: Could not find old net_from_sequence and pnl_from_sequence definitions.")

# 2. Insert Broker Switcher and Dhan Credentials Expander
old_header = 'st.sidebar.header("Algo Command Center")'
new_header = """st.sidebar.header("Algo Command Center")

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
                st.error("Failed to generate Dhan token. Check logs or credentials.")"""

if old_header in content:
    content = content.replace(old_header, new_header)
    print("SUCCESS: Inserted Broker selection and Dhan Credentials.")
else:
    print("WARNING: Could not find old st.sidebar.header.")

# 3. Update Live Authentication section for Dhan/Upstox
old_live_auth = """st.sidebar.subheader("🟢 Live Authentication")
if not api_key or not api_secret:
    st.sidebar.error("⚠️ API_KEY and API_SECRET missing in settings.json")
else:
    with st.sidebar.expander("🔑 Generate Daily Live Token", expanded=False):"""

new_live_auth = """st.sidebar.subheader("🟢 Live Authentication")
if broker_choice == "DHAN":
    if settings.get("DHAN_CLIENT_ID") and settings.get("DHAN_ACCESS_TOKEN"):
        st.sidebar.success("Dhan is configured as the active broker.")
    else:
        st.sidebar.warning("Add DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN before starting live Dhan mode.")
elif not api_key or not api_secret:
    st.sidebar.error("⚠️ API_KEY and API_SECRET missing in settings.json")
else:
    with st.sidebar.expander("🔑 Generate Daily Upstox Token", expanded=False):"""

if old_live_auth in content:
    content = content.replace(old_live_auth, new_live_auth)
    print("SUCCESS: Updated Live Authentication section.")
else:
    print("WARNING: Could not find old Live Authentication section.")

# 4. Update Unified Trading Parameters form
start_idx = content.find('with st.sidebar.form("config_form"):')
end_search = 'st.sidebar.success("Settings Saved!")'
end_idx = content.find(end_search, start_idx)
if start_idx != -1 and end_idx != -1:
    end_idx += len(end_search)
    old_form = content[start_idx:end_idx]
    
    new_form = """with st.sidebar.form("config_form"):
    env_mode = st.selectbox("Environment", ["SANDBOX", "LIVE"], index=0 if settings.get("ENVIRONMENT") == "SANDBOX" else 1)
    virtual_capital = float(settings.get("VIRTUAL_CAPITAL", config.VIRTUAL_CAPITAL))
    if env_mode == "SANDBOX":
        virtual_capital = st.number_input("Virtual Capital Allocation", value=virtual_capital, step=10000.0, min_value=0.0)
    else:
        st.caption("Live mode uses broker funds for sizing.")
    
    st.markdown("**MTF-OBLT Parameters**")
    
    mtf_strategy_type = st.selectbox("Strategy Type", ["Ratio", "Synthetic Future"], index=0 if settings.get("MTF_STRATEGY_TYPE", config.MTF_STRATEGY_TYPE) == "Ratio" else 1)
    mtf_trigger_type = st.selectbox("Trigger Type", ["choch", "bos"], index=0 if settings.get("MTF_TRIGGER_TYPE", config.MTF_TRIGGER_TYPE) == "choch" else 1)
    mtf_stop_loss_type = st.selectbox("Stop Loss Type", ["5m_origin", "ob_low"], index=0 if settings.get("MTF_STOP_LOSS_TYPE", config.MTF_STOP_LOSS_TYPE) == "5m_origin" else 1)
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
        
        settings["NIFTY_EXPIRY"] = str(nifty_exp)
        settings["SENSEX_EXPIRY"] = str(sensex_exp)
        
        save_settings(settings)
        st.sidebar.success("Settings Saved!")"""
        
    content = content.replace(old_form, new_form)
    print("SUCCESS: Updated Unified Settings config form.")
else:
    print("WARNING: Could not find config_form in dashboard.py.")

# 5. Update Live Position Tracker Table
old_table_code = """            st.markdown("### Live Position Tracker")
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
            
            st.dataframe(df_live.style.applymap(color_pnl, subset=['PnL / Point']), hide_index=True, width='stretch')"""

new_table_code = """            st.markdown("### Live Position Tracker")
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
            
            st.dataframe(df_live.style.applymap(color_pnl, subset=['PnL Contribution / Pt']), hide_index=True, width='stretch')"""

if old_table_code in content:
    content = content.replace(old_table_code, new_table_code)
    print("SUCCESS: Updated Live Position Tracker.")
else:
    print("WARNING: Could not find old table code in dashboard.py.")

# 6. Update P&L calculator calls with strategy_type parameter
content = content.replace(
    "entry_net = net_from_sequence(entries, sequence)",
    'entry_net = net_from_sequence(entries, sequence, strategy_type=state.get("strategy_type", ""))'
)
content = content.replace(
    "live_net = net_from_sequence(live_leg_prices, sequence)",
    'live_net = net_from_sequence(live_leg_prices, sequence, strategy_type=state.get("strategy_type", ""))'
)
content = content.replace(
    "gross_pnl = pnl_from_sequence(entries, live_leg_prices, qty, sequence)",
    'gross_pnl = pnl_from_sequence(entries, live_leg_prices, qty, sequence, strategy_type=state.get("strategy_type", ""))'
)
print("SUCCESS: Updated P&L calculator calls.")

# 7. Add tab support and indentation for tab_dashboard
title_str = 'st.title("ALGO COMMAND CENTER")'
title_idx = content.find(title_str)

if title_idx != -1:
    split_idx = title_idx + len(title_str)
    header_part = content[:split_idx]
    body_part = content[split_idx:]
    
    refresh_idx = body_part.find('auto_refresh = st.toggle')
    if refresh_idx != -1:
        dashboard_body = body_part[:refresh_idx]
        refresh_body = body_part[refresh_idx:]
        
        # Indent the dashboard_body
        indented_body = ""
        for line in dashboard_body.splitlines(keepends=True):
            if line.strip():
                indented_body += "    " + line
            else:
                indented_body += line
                
        # Build the new body with tab definitions
        new_body = f"""

tab_dashboard, tab_scanner = st.tabs(["📊 Performance Dashboard", "🔍 Market Scanner"])

with tab_dashboard:
{indented_body}
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
                def style_scanner(row):
                    color = ''
                    if row['trigger'] == 'CONFIRMED':
                        color = 'background-color: rgba(16, 185, 129, 0.2)'
                    elif row['pullback'] == 'PENDING':
                        color = 'background-color: rgba(59, 130, 246, 0.1)'
                    return [color] * len(row)
                st.dataframe(df_scan.style.apply(style_scanner, axis=1), hide_index=True, width='stretch')
            else:
                st.info("No symbols currently scanned.")
        except Exception as e:
            st.error(f"Error loading screener state: {{e}}")
    else:
        st.info("Screener file not found. Make sure the trading engine is running and scanning.")

"""
        
        content = header_part + new_body + refresh_body
        print("SUCCESS: Added tabs and wrapped dashboard body.")
    else:
        print("WARNING: Could not find auto_refresh block in dashboard.py.")
else:
    print("WARNING: Could not find title in dashboard.py.")

with open(dashboard_path, "w", encoding="utf-8") as f:
    f.write(content)

print("Dashboard update completed.")
