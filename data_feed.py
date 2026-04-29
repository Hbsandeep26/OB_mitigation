# data_feed.py
import json
import logging
import os
import tempfile
import time

import config
from broker import get_broker

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def get_spot_price(index_symbol):
    logging.info("Fetching live spot price for %s...", index_symbol)
    try:
        live_price = get_broker().get_spot_price(index_symbol)
        if live_price:
            logging.info("Live Spot Price for %s is: %s", index_symbol, live_price)
            return live_price
        logging.error("Failed to parse spot price for %s.", index_symbol)
        return None
    except Exception as e:
        logging.error("Broker API error fetching spot price: %s", e)
        return None


def get_option_chain(index_symbol, expiry_date):
    logging.info("Fetching option chain for %s expiring on %s...", index_symbol, expiry_date)
    try:
        return get_broker().get_option_chain(index_symbol, expiry_date)
    except Exception as e:
        logging.error("Broker API error fetching option chain: %s", e)
        return []


def get_fresh_option_quotes(instrument_keys_list):
    """Fetch real-time option LTPs before entry/exit accounting."""
    try:
        return get_broker().get_fresh_option_quotes(instrument_keys_list)
    except Exception as e:
        logging.error("Failed to fetch fresh quotes: %s", e)
        return {}


def get_india_vix():
    try:
        vix_value = get_broker().get_india_vix()
        if vix_value:
            logging.info("India VIX: %.2f", vix_value)
            return vix_value
        logging.warning("VIX data missing from broker response.")
        return None
    except Exception as e:
        logging.error("Failed to fetch India VIX: %s", e)
        return None


def get_spot_with_ohlc(index_symbol):
    try:
        ltp, prev_close = get_broker().get_spot_with_ohlc(index_symbol)
        if ltp and prev_close:
            logging.info("%s LTP: %s, Prev Close: %s", index_symbol, ltp, prev_close)
            return ltp, prev_close
        logging.warning("OHLC data missing for %s", index_symbol)
        return None, None
    except Exception as e:
        logging.error("Failed to fetch OHLC data for %s: %s", index_symbol, e)
        return None, None


def _atomic_write_json(path, payload):
    temp_fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(path))
    with os.fdopen(temp_fd, "w") as f:
        json.dump(payload, f)
    for _ in range(5):
        try:
            os.replace(temp_path, path)
            return
        except PermissionError:
            time.sleep(0.05)


def monitor_live_prices(instrument_keys_dict, callback_function):
    logging.info("Initializing broker WebSocket connection for live risk management...")

    import state_manager

    trade_state = state_manager.load_state()
    index_symbol = trade_state.get("index_symbol", "NIFTY") if trade_state else "NIFTY"

    spot_key = "NSE_INDEX|Nifty 50" if index_symbol == "NIFTY" else "BSE_INDEX|SENSEX"
    vix_key = "NSE_INDEX|India VIX"

    keys_to_subscribe = list(instrument_keys_dict.values())
    keys_to_subscribe.append(spot_key)
    keys_to_subscribe.append(vix_key)

    streamer = get_broker().make_streamer(keys_to_subscribe)

    ws_state = {
        "stop_loss_hit": False,
        "error_count": 0,
        "exit_prices": {},
        "latest_prices": {},
        "last_tick_time": time.time(),
        "last_write_time": 0,
    }

    def on_message(message):
        try:
            ws_state["error_count"] = 0
            tick_received_at = time.time()

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
                    ws_state["last_tick_time"] = tick_received_at
                    ws_state["latest_prices"][instrument_key] = {"ltp": ltp, "ts": tick_received_at}

            if ws_state["latest_prices"]:
                if time.time() - ws_state["last_write_time"] > 1.0:
                    _atomic_write_json(os.path.join(BASE_DIR, "live_prices.json"), ws_state["latest_prices"])
                    ws_state["last_write_time"] = time.time()

                stop_loss_triggered, current_prices = callback_function(
                    ws_state["latest_prices"], instrument_keys_dict
                )
                if stop_loss_triggered:
                    logging.critical("Exit Signal Received: %s. Terminating WebSocket.", stop_loss_triggered)
                    ws_state["stop_loss_hit"] = stop_loss_triggered
                    ws_state["exit_prices"] = current_prices
                    streamer.disconnect()

        except ValueError as e:
            logging.warning("Risk evaluation skipped due to stale/incomplete data: %s", e)
        except Exception as e:
            logging.error("Error parsing live tick data: %s", e)

    def on_error(error):
        logging.error("WebSocket Error: %s", error)
        ws_state["error_count"] += 1

        if ws_state["error_count"] >= 5:
            logging.critical("CRITICAL: Maximum WebSocket failures reached. Marking socket dead.")
            ws_state["stop_loss_hit"] = "SOCKET_DEAD"
            streamer.disconnect()

    streamer.on("message", on_message)
    streamer.on("error", on_error)

    logging.info("WebSocket connected. Streaming live market data...")
    streamer.connect()

    while not ws_state["stop_loss_hit"]:
        time.sleep(1)
        import datetime

        now = datetime.datetime.now()

        if now.hour > 15 or (now.hour == 15 and now.minute >= 30):
            logging.info("Market closed. Terminating WebSocket gracefully to preserve BTST state.")
            ws_state["stop_loss_hit"] = "MARKET_CLOSED"
            streamer.disconnect()
            break

        if time.time() - ws_state["last_tick_time"] > config.WEBSOCKET_SILENT_SECONDS:
            if now.hour == 15 and now.minute == 29:
                pass
            else:
                logging.critical("No WebSocket ticks received for %.0f seconds.", config.WEBSOCKET_SILENT_SECONDS)
                ws_state["stop_loss_hit"] = "SOCKET_DEAD"
                streamer.disconnect()
                break

    return ws_state["stop_loss_hit"], ws_state.get("exit_prices", {})
