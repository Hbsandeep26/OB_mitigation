# data_feed.py
import config
import logging
import requests
import urllib.parse
import upstox_client
import json
import time
import os
import tempfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def get_spot_price(index_symbol):
    logging.info(f"Fetching live spot price for {index_symbol}...")
    
    if index_symbol == "NIFTY":
        instrument_key = "NSE_INDEX|Nifty 50"
    elif index_symbol == "SENSEX":
        instrument_key = "BSE_INDEX|SENSEX"
    else:
        logging.error("Invalid Index Symbol provided.")
        return None

    url = 'https://api.upstox.com/v2/market-quote/quotes'
    safe_instrument_key = urllib.parse.quote(instrument_key)
    full_url = f"{url}?instrument_key={safe_instrument_key}"
    
    headers = {
        'accept': 'application/json',
        'Api-Version': '2.0',
        'Authorization': f'Bearer {config.get_live_token()}'
    }

    try:
        response = requests.get(full_url, headers=headers, timeout=5)
        response.raise_for_status() 
        data = response.json()
        
        response_key = instrument_key.replace('|', ':')
        
        if 'data' in data and response_key in data['data']:
            live_price = data['data'][response_key]['last_price']
            logging.info(f"Live Spot Price for {index_symbol} is: {live_price}")
            return live_price
        else:
            logging.error(f"Failed to parse price. Raw Data: {data}")
            return None

    except Exception as e:
        logging.error(f"Upstox API Error fetching spot price: {e}")
        return None

def get_option_chain(index_symbol, expiry_date):
    logging.info(f"Fetching option chain for {index_symbol} expiring on {expiry_date}...")
    
    if index_symbol == "NIFTY":
        instrument_key = "NSE_INDEX|Nifty 50"
    elif index_symbol == "SENSEX":
        instrument_key = "BSE_INDEX|SENSEX"
    else:
        return []

    url = 'https://api.upstox.com/v2/option/chain'
    safe_instrument_key = urllib.parse.quote(instrument_key)
    full_url = f"{url}?instrument_key={safe_instrument_key}&expiry_date={expiry_date}"
    
    headers = {
        'accept': 'application/json',
        'Api-Version': '2.0',
        'Authorization': f'Bearer {config.get_live_token()}'
    }

    try:
        response = requests.get(full_url, headers=headers, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        if 'data' in data:
            return data['data'] 
        else:
            logging.error(f"Option chain data missing. Raw Response: {data}")
            return []
            
    except Exception as e:
        logging.error(f"Upstox API Error fetching option chain: {e}")
        return []

def get_fresh_option_quotes(instrument_keys_list):
    """
    Bypasses the cached Option Chain and hits the Live Quotes API to get absolute real-time LTPs before entry.
    """
    url = 'https://api.upstox.com/v2/market-quote/quotes'
    keys_str = ",".join([urllib.parse.quote(k) for k in instrument_keys_list])
    full_url = f"{url}?instrument_key={keys_str}"
    
    headers = {
        'accept': 'application/json',
        'Api-Version': '2.0',
        'Authorization': f'Bearer {config.get_live_token()}'
    }
    try:
        response = requests.get(full_url, headers=headers, timeout=5)
        response.raise_for_status()
        data = response.json()
        fresh_prices = {}
        if 'data' in data:
            for key, val in data['data'].items():
                original_key = key.replace(':', '|')
                fresh_prices[original_key] = val.get('last_price', 0.0)
        return fresh_prices
    except Exception as e:
        logging.error(f"Failed to fetch fresh quotes: {e}")
        return {}

def get_india_vix():
    """
    Fetches the current India VIX value for VIX Adaptive Session Profiles.
    Returns VIX as a float, or None on failure.
    """
    instrument_key = "NSE_INDEX|India VIX"
    url = 'https://api.upstox.com/v2/market-quote/quotes'
    safe_key = urllib.parse.quote(instrument_key)
    full_url = f"{url}?instrument_key={safe_key}"
    
    headers = {
        'accept': 'application/json',
        'Api-Version': '2.0',
        'Authorization': f'Bearer {config.get_live_token()}'
    }
    
    try:
        response = requests.get(full_url, headers=headers, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        response_key = instrument_key.replace('|', ':')
        if 'data' in data and response_key in data['data']:
            vix_value = data['data'][response_key]['last_price']
            logging.info(f"📊 India VIX: {vix_value:.2f}")
            return vix_value
        else:
            logging.warning(f"VIX data missing from API response. Raw: {data}")
            return None
    except Exception as e:
        logging.error(f"Failed to fetch India VIX: {e}")
        return None

def get_spot_with_ohlc(index_symbol):
    """
    Fetches both the live spot price AND previous day's close for the Opening Range Gap Filter.
    Returns (ltp, previous_close) tuple, or (None, None) on failure.
    """
    if index_symbol == "NIFTY":
        instrument_key = "NSE_INDEX|Nifty 50"
    elif index_symbol == "SENSEX":
        instrument_key = "BSE_INDEX|SENSEX"
    else:
        return None, None

    url = 'https://api.upstox.com/v2/market-quote/quotes'
    safe_key = urllib.parse.quote(instrument_key)
    full_url = f"{url}?instrument_key={safe_key}"
    
    headers = {
        'accept': 'application/json',
        'Api-Version': '2.0',
        'Authorization': f'Bearer {config.get_live_token()}'
    }
    
    try:
        response = requests.get(full_url, headers=headers, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        response_key = instrument_key.replace('|', ':')
        if 'data' in data and response_key in data['data']:
            quote = data['data'][response_key]
            ltp = quote.get('last_price', 0.0)
            # Upstox returns OHLC with 'close' being previous day's close
            prev_close = quote.get('ohlc', {}).get('close', 0.0)
            logging.info(f"📈 {index_symbol} LTP: {ltp}, Prev Close: {prev_close}")
            return ltp, prev_close
        else:
            logging.warning(f"OHLC data missing for {index_symbol}")
            return None, None
    except Exception as e:
        logging.error(f"Failed to fetch OHLC data for {index_symbol}: {e}")
        return None, None

def monitor_live_prices(instrument_keys_dict, callback_function):
    logging.info("Initializing SDK WebSocket connection for live risk management...")
    
    import state_manager
    # We load this ONCE strictly to extract the index_symbol. 
    trade_state = state_manager.load_state()
    index_symbol = trade_state.get('index_symbol', 'NIFTY') if trade_state else 'NIFTY'
    
    spot_key = "NSE_INDEX|Nifty 50" if index_symbol == "NIFTY" else "BSE_INDEX|SENSEX"
    vix_key = "NSE_INDEX|India VIX"
    
    keys_to_subscribe = list(instrument_keys_dict.values())
    keys_to_subscribe.append(spot_key) 
    keys_to_subscribe.append(vix_key) 
    
    configuration = upstox_client.Configuration()
    configuration.access_token = config.get_live_token()
    api_client = upstox_client.ApiClient(configuration)
    
    streamer = upstox_client.MarketDataStreamerV3(api_client, keys_to_subscribe, "full")
    
    # --- BUG 6 FIX: Renamed local tracking dictionary to `ws_state` ---
    ws_state = {
        "stop_loss_hit": False, 
        "error_count": 0, 
        "exit_prices": {}, 
        "latest_prices": {}, 
        "last_tick_time": time.time(), 
        "last_write_time": 0
    }

    def on_message(message):
        try:
            ws_state["error_count"] = 0 
            ws_state["last_tick_time"] = time.time() 
            
            if isinstance(message, str):
                message = json.loads(message)
                
            feeds = message.get("feeds", {})
                    
            for instrument_key, feed_data in feeds.items():
                ltp = 0.0
                
                if "fullFeed" in feed_data:
                    ff = feed_data["fullFeed"]
                    if "marketFF" in ff:
                        ltp = ff["marketFF"].get("ltpc", {}).get("ltp", 0.0)
                    elif "indexFF" in ff:
                        ltp = ff["indexFF"].get("ltpc", {}).get("ltp", 0.0)
                elif "ltpc" in feed_data:
                    ltp = feed_data.get("ltpc", {}).get("ltp", 0.0)
                    
                if ltp > 0:
                    ws_state["latest_prices"][instrument_key] = {'ltp': ltp}

            if ws_state["latest_prices"]:
                if time.time() - ws_state["last_write_time"] > 1.0:
                    live_prices_path = os.path.join(BASE_DIR, "live_prices.json")
                    temp_fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(live_prices_path))
                    with os.fdopen(temp_fd, 'w') as f:
                        json.dump(ws_state["latest_prices"], f)
                    os.replace(temp_path, live_prices_path)
                    ws_state["last_write_time"] = time.time()
                    
                stop_loss_triggered, current_prices = callback_function(ws_state["latest_prices"], instrument_keys_dict)
                
                if stop_loss_triggered:
                    logging.critical(f"Exit Signal Received: {stop_loss_triggered}. Terminating WebSocket.")
                    ws_state["stop_loss_hit"] = stop_loss_triggered
                    ws_state["exit_prices"] = current_prices
                    streamer.disconnect() 
                    
        except Exception as e:
            logging.error(f"Error parsing live tick data: {e}")

    def on_error(error):
        logging.error(f"WebSocket Error: {error}")
        ws_state["error_count"] += 1
        
        if ws_state["error_count"] >= 5:
            logging.critical("CRITICAL: Maximum WebSocket failures reached. Initiating emergency square-off!")
            ws_state["stop_loss_hit"] = True 
            streamer.disconnect()

    streamer.on("message", on_message)
    streamer.on("error", on_error)
    
    logging.info("WebSocket Connected! Streaming live market data...")
    
    streamer.connect()
    
    while not ws_state["stop_loss_hit"]:
        time.sleep(1)
        import datetime
        now = datetime.datetime.now()
        
        if now.hour > 15 or (now.hour == 15 and now.minute >= 30):
            logging.info("🏁 Market Closed. Terminating WebSocket gracefully to preserve BTST state.")
            ws_state["stop_loss_hit"] = "MARKET_CLOSED"
            streamer.disconnect()
            break

        if time.time() - ws_state["last_tick_time"] > 60.0:
            if now.hour == 15 and now.minute == 29:
                pass 
            else:
                logging.critical("☠️ SILENT DEATH DETECTED: No WebSocket ticks received for 60 seconds! Forcing emergency exit.")
                ws_state["stop_loss_hit"] = "SOCKET_DEAD"
                streamer.disconnect()
                break

    return ws_state["stop_loss_hit"], ws_state.get("exit_prices", {})
