import logging
import threading
import time
import urllib.parse
from collections import defaultdict
from datetime import datetime, time as dt_time, timedelta
from types import SimpleNamespace

import requests
import upstox_client

import config


class UpstoxBroker:
    """Small adapter for all Upstox-specific market data and order APIs."""

    def _headers(self):
        return {
            "accept": "application/json",
            "Api-Version": "2.0",
            "Authorization": f"Bearer {config.get_live_token()}",
        }

    @staticmethod
    def index_key(index_symbol):
        if index_symbol == "NIFTY":
            return "NSE_INDEX|Nifty 50"
        if index_symbol == "SENSEX":
            return "BSE_INDEX|SENSEX"
        raise ValueError(f"Invalid index symbol: {index_symbol}")

    @staticmethod
    def response_key(instrument_key):
        return instrument_key.replace("|", ":")

    def get_quote_payload(self, instrument_keys):
        url = "https://api.upstox.com/v2/market-quote/quotes"
        keys_str = ",".join(urllib.parse.quote(key) for key in instrument_keys)
        response = requests.get(f"{url}?instrument_key={keys_str}", headers=self._headers(), timeout=5)
        response.raise_for_status()
        return response.json().get("data", {})

    def get_spot_price(self, index_symbol):
        instrument_key = self.index_key(index_symbol)
        data = self.get_quote_payload([instrument_key])
        quote = data.get(self.response_key(instrument_key), {})
        return quote.get("last_price")

    def get_spot_with_ohlc(self, index_symbol):
        instrument_key = self.index_key(index_symbol)
        data = self.get_quote_payload([instrument_key])
        quote = data.get(self.response_key(instrument_key), {})
        return quote.get("last_price"), quote.get("ohlc", {}).get("close")

    def get_india_vix(self):
        instrument_key = "NSE_INDEX|India VIX"
        data = self.get_quote_payload([instrument_key])
        quote = data.get(self.response_key(instrument_key), {})
        return quote.get("last_price")

    def get_intraday_candles(self, index_symbol, minutes=15):
        instrument_key = self.index_key(index_symbol)
        safe_key = urllib.parse.quote(instrument_key)
        url = f"https://api.upstox.com/v3/historical-candle/intraday/{safe_key}/minutes/{int(minutes)}"
        response = requests.get(url, headers=self._headers(), timeout=5)
        response.raise_for_status()
        return response.json().get("data", {}).get("candles", [])

    def get_option_chain(self, index_symbol, expiry_date):
        instrument_key = self.index_key(index_symbol)
        url = "https://api.upstox.com/v2/option/chain"
        safe_key = urllib.parse.quote(instrument_key)
        response = requests.get(
            f"{url}?instrument_key={safe_key}&expiry_date={expiry_date}",
            headers=self._headers(),
            timeout=5,
        )
        response.raise_for_status()
        return response.json().get("data", [])

    def get_funds_v3(self):
        url = "https://api.upstox.com/v3/user/get-funds-and-margin"
        headers = self._headers()
        headers["Api-Version"] = "3.0"
        response = requests.get(url, headers=headers, timeout=5)
        response.raise_for_status()
        return response.json()

    def get_funds_v2(self):
        url = "https://api.upstox.com/v2/user/get-funds-and-margin"
        response = requests.get(url, headers=self._headers(), timeout=5)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _extract_v3_available_margin(payload):
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        available = data.get("available_to_trade", {}) or {}
        return float(available.get("total", 0.0) or 0.0)

    @staticmethod
    def _extract_v2_available_margin(payload):
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        equity = data.get("equity", {}) or {}
        return float(equity.get("available_margin", 0.0) or 0.0)

    def get_available_margin(self):
        try:
            payload = self.get_funds_v3()
            available_margin = self._extract_v3_available_margin(payload)
            if available_margin > 0:
                return available_margin, {"source": "UPSTOX_FUNDS_V3"}
        except Exception as err:
            logging.warning("Upstox V3 funds fetch failed; trying V2: %s", err)

        payload = self.get_funds_v2()
        available_margin = self._extract_v2_available_margin(payload)
        if available_margin <= 0:
            raise ValueError("Upstox funds response has no available equity margin")
        return available_margin, {"source": "UPSTOX_FUNDS_V2"}

    def get_order_margin(self, instruments):
        url = "https://api.upstox.com/v2/charges/margin"
        response = requests.post(
            url,
            headers={**self._headers(), "Content-Type": "application/json"},
            json={"instruments": instruments},
            timeout=5,
        )
        response.raise_for_status()
        data = response.json().get("data", {}) or {}
        return data.get("final_margin") or data.get("required_margin") or 0.0

    def get_fresh_option_quotes(self, instrument_keys):
        data = self.get_quote_payload(instrument_keys)
        fresh_prices = {}
        for key, quote in data.items():
            fresh_prices[key.replace(":", "|")] = quote.get("last_price", 0.0)
        return fresh_prices

    def make_order_apis(self):
        configuration = upstox_client.Configuration()
        configuration.access_token = config.get_live_token()
        api_client = upstox_client.ApiClient(configuration)
        return upstox_client.OrderApiV3(api_client), upstox_client.OrderApi(api_client)

    def make_streamer(self, instrument_keys):
        configuration = upstox_client.Configuration()
        configuration.access_token = config.get_live_token()
        api_client = upstox_client.ApiClient(configuration)
        return upstox_client.MarketDataStreamerV3(api_client, instrument_keys, "full")


class DhanPollingStreamer:
    """Small event-emitting REST poller that matches the Upstox streamer shape.

    Dhan's native market-feed WebSocket returns binary frames. Until the binary
    feed is wired in, this poller keeps the existing risk monitor working with
    quote snapshots and the same `on("message", callback)` contract.
    """

    def __init__(self, broker, instrument_keys, poll_seconds=None):
        self.broker = broker
        self.instrument_keys = list(dict.fromkeys(instrument_keys or []))
        self.poll_seconds = float(poll_seconds or config._setting("DHAN_STREAM_POLL_SECONDS", 1.0))
        self.handlers = {}
        self._stop = threading.Event()
        self._thread = None

    def on(self, event_name, handler):
        self.handlers[event_name] = handler

    def connect(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop, name="dhan-rest-poller", daemon=True)
        self._thread.start()

    def disconnect(self):
        self._stop.set()

    def _emit_error(self, error):
        handler = self.handlers.get("error")
        if handler:
            handler(error)

    def _poll_loop(self):
        while not self._stop.is_set():
            try:
                quotes = self.broker.get_quote_payload(self.instrument_keys)
                now_ts = time.time()
                feeds = {}
                for key, quote in quotes.items():
                    ltp = float((quote or {}).get("last_price", 0.0) or 0.0)
                    if ltp <= 0:
                        continue
                    feeds[key] = {
                        "ltpc": {
                            "ltp": ltp,
                            "ltt": now_ts,
                        }
                    }
                if feeds and self.handlers.get("message"):
                    self.handlers["message"]({"feeds": feeds})
            except Exception as err:
                self._emit_error(err)
            self._stop.wait(self.poll_seconds)


class DhanOrderPlacementApi:
    def __init__(self, broker):
        self.broker = broker

    def place_order(self, body):
        return self.broker.place_order(body)


class DhanOrderStatusApi:
    def __init__(self, broker):
        self.broker = broker

    def get_order_status(self, order_id=None):
        return self.broker.get_order_status(order_id)


class DhanBroker:
    """Adapter for DhanHQ v2 market data, option chain, margin, and orders."""

    BASE_URL = "https://api.dhan.co/v2"
    TOKEN_PREFIX = "DHAN|"
    INDEXES = {
        "NIFTY": {"segment": "IDX_I", "security_id": "13", "option_segment": "NSE_FNO", "instrument": "INDEX"},
        "BANKNIFTY": {"segment": "IDX_I", "security_id": "25", "option_segment": "NSE_FNO", "instrument": "INDEX"},
        "SENSEX": {"segment": "IDX_I", "security_id": "51", "option_segment": "BSE_FNO", "instrument": "INDEX"},
    }

    LEGACY_INDEX_KEYS = {
        "NSE_INDEX|Nifty 50": ("IDX_I", "13"),
        "BSE_INDEX|SENSEX": ("IDX_I", "51"),
    }

    def _headers(self, include_client_id=True):
        token = config.get_dhan_access_token()
        client_id = config.get_dhan_client_id()
        if not token or not client_id:
            raise ValueError("DHAN_ACCESS_TOKEN and DHAN_CLIENT_ID must be configured for Dhan broker calls")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "access-token": token,
        }
        if include_client_id:
            headers["client-id"] = client_id
            headers["dhanClientId"] = client_id
        return headers

    @staticmethod
    def _sanitize_for_log(value):
        if isinstance(value, dict):
            sanitized = {}
            for key, item in value.items():
                key_text = str(key).lower()
                if any(secret in key_text for secret in ("token", "pin", "totp", "secret")):
                    sanitized[key] = "***REDACTED***"
                else:
                    sanitized[key] = DhanBroker._sanitize_for_log(item)
            return sanitized
        if isinstance(value, list):
            return [DhanBroker._sanitize_for_log(item) for item in value]
        return value

    @staticmethod
    def _response_text(response):
        try:
            return (response.text or "")[:1200]
        except Exception:
            return ""

    def _raise_for_status(self, response, method, path, payload=None):
        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError:
            body = self._response_text(response)
            sanitized_payload = self._sanitize_for_log(payload or {})
            message = (
                f"Dhan API {method} {path} failed "
                f"status={response.status_code} reason={response.reason} "
                f"body={body} payload={sanitized_payload}"
            )
            logging.error(message)
            raise requests.exceptions.HTTPError(message, response=response)

    def _get(self, path, include_client_id=False):
        max_retries = 3
        backoff = 1.5
        for attempt in range(max_retries):
            try:
                response = requests.get(
                    f"{self.BASE_URL}{path}",
                    headers=self._headers(include_client_id=include_client_id),
                    timeout=10,
                )
                if response.status_code == 429:
                    logging.warning(
                        "Dhan API returned 429 Too Many Requests on GET %s. Retrying attempt %d/%d after %.2fs...",
                        path, attempt + 1, max_retries, backoff
                    )
                    time.sleep(backoff)
                    backoff *= 1.5
                    continue
                self._raise_for_status(response, "GET", path)
                return response.json()
            except requests.exceptions.HTTPError as err:
                if err.response is not None and err.response.status_code == 429:
                    logging.warning(
                        "Dhan API raised 429 HTTPError on GET %s. Retrying attempt %d/%d after %.2fs...",
                        path, attempt + 1, max_retries, backoff
                    )
                    time.sleep(backoff)
                    backoff *= 1.5
                    continue
                raise
            except requests.exceptions.RequestException as err:
                logging.warning(
                    "Dhan API GET %s connection failed on attempt %d/%d: %s",
                    path, attempt + 1, max_retries, err
                )
                if attempt < max_retries - 1:
                    time.sleep(backoff)
                    backoff *= 1.5
                    continue
                raise
        # Final attempt
        response = requests.get(
            f"{self.BASE_URL}{path}",
            headers=self._headers(include_client_id=include_client_id),
            timeout=10,
        )
        self._raise_for_status(response, "GET", path)
        return response.json()

    def _post(self, path, payload, include_client_id=True):
        max_retries = 3
        backoff = 1.5
        for attempt in range(max_retries):
            try:
                response = requests.post(
                    f"{self.BASE_URL}{path}",
                    headers=self._headers(include_client_id=include_client_id),
                    json=payload,
                    timeout=15,
                )
                if response.status_code == 429:
                    logging.warning(
                        "Dhan API returned 429 Too Many Requests on POST %s. Retrying attempt %d/%d after %.2fs...",
                        path, attempt + 1, max_retries, backoff
                    )
                    time.sleep(backoff)
                    backoff *= 1.5
                    continue
                self._raise_for_status(response, "POST", path, payload)
                return response.json()
            except requests.exceptions.HTTPError as err:
                if err.response is not None and err.response.status_code == 429:
                    logging.warning(
                        "Dhan API raised 429 HTTPError on POST %s. Retrying attempt %d/%d after %.2fs...",
                        path, attempt + 1, max_retries, backoff
                    )
                    time.sleep(backoff)
                    backoff *= 1.5
                    continue
                raise
            except requests.exceptions.RequestException as err:
                logging.warning(
                    "Dhan API POST %s connection failed on attempt %d/%d payload=%s error=%s",
                    path,
                    attempt + 1,
                    max_retries,
                    self._sanitize_for_log(payload),
                    err,
                )
                if attempt < max_retries - 1:
                    time.sleep(backoff)
                    backoff *= 1.5
                    continue
                raise
        # Final attempt
        response = requests.post(
            f"{self.BASE_URL}{path}",
            headers=self._headers(include_client_id=include_client_id),
            json=payload,
            timeout=15,
        )
        self._raise_for_status(response, "POST", path, payload)
        return response.json()

    @classmethod
    def instrument_key(cls, segment, security_id):
        return f"{cls.TOKEN_PREFIX}{segment}|{security_id}"

    @classmethod
    def _index_meta(cls, index_symbol):
        sym_upper = str(index_symbol).upper().strip()
        if sym_upper in cls.INDEXES:
            return cls.INDEXES[sym_upper]
        
        try:
            from liquidity_universe import select_universe
            found = select_universe(sym_upper)
            if found:
                meta = found[0]
                return {
                    "segment": meta["exchange_segment"],
                    "security_id": meta["security_id"],
                    "option_segment": "NSE_FNO" if meta["asset_class"] == "EQUITY" else "NSE_FNO",
                    "instrument": meta["instrument"],
                }
        except Exception as err:
            logging.debug("Error looking up symbol %s in liquidity universe: %s", index_symbol, err)
            
        raise ValueError(f"Invalid Dhan index or stock symbol: {index_symbol}")

    @staticmethod
    def _is_trading_day(day):
        holiday_set = set(getattr(config, "MARKET_HOLIDAYS", []) or [])
        return day.weekday() < 5 and day.strftime("%Y-%m-%d") not in holiday_set

    @classmethod
    def _last_market_datetime(cls, now=None):
        now = now or datetime.now()
        session_start = now.replace(hour=9, minute=15, second=0, microsecond=0)
        session_end = now.replace(hour=15, minute=30, second=0, microsecond=0)
        if cls._is_trading_day(now.date()) and session_start <= now <= session_end:
            return now
        if cls._is_trading_day(now.date()) and now > session_end:
            return session_end

        cursor_day = now.date() - timedelta(days=1)
        while not cls._is_trading_day(cursor_day):
            cursor_day = cursor_day - timedelta(days=1)
        return datetime.combine(cursor_day, dt_time(15, 30))


    def _parse_instrument_key(self, instrument_key):
        key = str(instrument_key)
        if key in self.LEGACY_INDEX_KEYS:
            return self.LEGACY_INDEX_KEYS[key]

        india_vix_key = "NSE_INDEX|India VIX"
        if key == india_vix_key:
            security_id = str(config._setting("DHAN_INDIA_VIX_SECURITY_ID", "") or "").strip()
            if not security_id:
                raise ValueError("DHAN_INDIA_VIX_SECURITY_ID is not configured")
            return "IDX_I", security_id

        if key.startswith(self.TOKEN_PREFIX):
            _, segment, security_id = key.split("|", 2)
            return segment, str(security_id)

        first, sep, second = key.partition("|")
        if sep and first in {"IDX_I", "NSE_EQ", "BSE_EQ", "NSE_FNO", "BSE_FNO", "MCX_COMM"}:
            return first, second

        raise ValueError(f"Unsupported Dhan instrument key: {instrument_key}")

    def _securities_payload(self, instrument_keys):
        grouped = defaultdict(list)
        reverse = defaultdict(list)
        for key in instrument_keys or []:
            try:
                segment, security_id = self._parse_instrument_key(key)
            except ValueError as err:
                logging.debug("Skipping Dhan quote key %s: %s", key, err)
                continue
            grouped[segment].append(int(security_id))
            reverse[(segment, str(security_id))].append(key)
        return {segment: sorted(set(ids)) for segment, ids in grouped.items()}, reverse

    def get_quote_payload(self, instrument_keys):
        payload, reverse = self._securities_payload(instrument_keys)
        if not payload:
            return {}
        data = self._post("/marketfeed/quote", payload).get("data", {}) or {}
        quotes = {}
        for (segment, security_id), original_keys in reverse.items():
            segment_data = data.get(segment, {}) or {}
            quote = segment_data.get(str(security_id)) or segment_data.get(int(security_id)) or {}
            for original_key in original_keys:
                quotes[original_key] = quote
        return quotes

    def _single_ltp(self, segment, security_id):
        payload = {segment: [int(security_id)]}
        data = self._post("/marketfeed/ltp", payload).get("data", {}) or {}
        quote = (data.get(segment, {}) or {}).get(str(security_id)) or {}
        return quote.get("last_price")

    def get_spot_price(self, index_symbol):
        meta = self._index_meta(index_symbol)
        return self._single_ltp(meta["segment"], meta["security_id"])

    def get_spot_with_ohlc(self, index_symbol):
        meta = self._index_meta(index_symbol)
        payload = {meta["segment"]: [int(meta["security_id"])]}
        data = self._post("/marketfeed/ohlc", payload).get("data", {}) or {}
        quote = (data.get(meta["segment"], {}) or {}).get(str(meta["security_id"])) or {}
        return quote.get("last_price"), (quote.get("ohlc", {}) or {}).get("close")

    def get_india_vix(self):
        security_id = str(config._setting("DHAN_INDIA_VIX_SECURITY_ID", "") or "").strip()
        if not security_id:
            logging.warning("DHAN_INDIA_VIX_SECURITY_ID is not configured; VIX unavailable from Dhan.")
            return None
        return self._single_ltp("IDX_I", security_id)

    def get_intraday_candles(self, index_symbol, minutes=15, from_date=None):
        meta = self._index_meta(index_symbol)
        to_dt = self._last_market_datetime()
        from_dt = from_date if from_date is not None else to_dt.replace(hour=9, minute=15, second=0, microsecond=0)
        if from_dt > to_dt:
            from_dt = to_dt.replace(hour=9, minute=15, second=0, microsecond=0)
        payload = {
            "securityId": str(meta["security_id"]),
            "exchangeSegment": meta["segment"],
            "instrument": meta["instrument"],
            "interval": str(int(minutes)),
            "oi": False,
            "fromDate": from_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "toDate": to_dt.strftime("%Y-%m-%d %H:%M:%S"),
        }
        return self._charts_to_candles(self._post("/charts/intraday", payload))


    @staticmethod
    def _charts_to_candles(payload):
        opens = payload.get("open", []) or []
        highs = payload.get("high", []) or []
        lows = payload.get("low", []) or []
        closes = payload.get("close", []) or []
        volumes = payload.get("volume", []) or []
        timestamps = payload.get("timestamp", []) or []
        open_interest = payload.get("open_interest", []) or []
        candles = []
        for idx, ts in enumerate(timestamps):
            try:
                candles.append([
                    ts,
                    float(opens[idx]),
                    float(highs[idx]),
                    float(lows[idx]),
                    float(closes[idx]),
                    float(volumes[idx]) if idx < len(volumes) else 0.0,
                    float(open_interest[idx]) if idx < len(open_interest) else 0.0,
                ])
            except (IndexError, TypeError, ValueError):
                continue
        return candles

    def _convert_option_leg(self, leg, option_segment):
        if not leg:
            return {}
        security_id = str(leg.get("security_id") or "")
        oi = float(leg.get("oi", 0.0) or 0.0)
        previous_oi = float(leg.get("previous_oi", 0.0) or 0.0)
        return {
            "instrument_key": self.instrument_key(option_segment, security_id),
            "security_id": security_id,
            "market_data": {
                "ltp": float(leg.get("last_price", 0.0) or 0.0),
                "oi": oi,
                "previous_oi": previous_oi,
                "change_oi": oi - previous_oi,
                "volume": float(leg.get("volume", 0.0) or 0.0),
                "previous_volume": float(leg.get("previous_volume", 0.0) or 0.0),
                "average_price": float(leg.get("average_price", 0.0) or 0.0),
                "bid_price": float(leg.get("top_bid_price", 0.0) or 0.0),
                "ask_price": float(leg.get("top_ask_price", 0.0) or 0.0),
                "bid_qty": int(leg.get("top_bid_quantity", 0) or 0),
                "ask_qty": int(leg.get("top_ask_quantity", 0) or 0),
                "iv": float(leg.get("implied_volatility", 0.0) or 0.0),
            },
            "greeks": leg.get("greeks", {}) or {},
        }

    def get_option_chain(self, index_symbol, expiry_date):
        meta = self._index_meta(index_symbol)
        payload = {
            "UnderlyingScrip": int(meta["security_id"]),
            "UnderlyingSeg": meta["segment"],
            "Expiry": str(expiry_date),
        }
        raw = self._post("/optionchain", payload).get("data", {}) or {}
        spot = raw.get("last_price")
        option_segment = meta["option_segment"]
        chain = []
        for strike_text, row in sorted((raw.get("oc", {}) or {}).items(), key=lambda item: float(item[0])):
            try:
                strike = float(strike_text)
            except (TypeError, ValueError):
                continue
            chain.append({
                "strike_price": strike,
                "underlying_spot_price": spot,
                "call_options": self._convert_option_leg((row or {}).get("ce"), option_segment),
                "put_options": self._convert_option_leg((row or {}).get("pe"), option_segment),
            })
        return chain

    def get_available_margin(self):
        payload = self._get("/fundlimit")
        available = (
            payload.get("availabelBalance")
            or payload.get("availableBalance")
            or payload.get("withdrawableBalance")
            or 0.0
        )
        available = float(available or 0.0)
        if available <= 0:
            raise ValueError("Dhan fundlimit response has no available balance")
        return available, {"source": "DHAN_FUNDLIMIT"}

    @staticmethod
    def _product_type(product):
        product = str(product or "").upper()
        return "INTRADAY" if product in {"I", "INTRADAY"} else product or "INTRADAY"

    def get_order_margin(self, instruments):
        scripts = []
        for item in instruments or []:
            token = item.get("instrument_key")
            if not token:
                continue
            segment, security_id = self._parse_instrument_key(token)
            scripts.append({
                "exchangeSegment": segment,
                "transactionType": str(item.get("transaction_type", "BUY")).upper(),
                "quantity": int(item.get("quantity", 0) or 0),
                "productType": self._product_type(item.get("product", "I")),
                "securityId": str(security_id),
                "price": float(item.get("price", 0.0) or 0.0),
                "triggerPrice": float(item.get("trigger_price", 0.0) or 0.0),
            })
        if not scripts:
            return 0.0

        payload = {
            "includePosition": True,
            "includeOrders": True,
            "dhanClientId": config.get_dhan_client_id(),
            "scripts": scripts,
        }
        response = self._post("/margincalculator/multi", payload)
        data = response.get("data", response) if isinstance(response, dict) else {}
        for key in ("totalMargin", "total_margin", "totalMarginRequired", "total_margin_required"):
            if key in data:
                return float(data.get(key) or 0.0)
        return 0.0

    def get_fresh_option_quotes(self, instrument_keys):
        quotes = self.get_quote_payload(instrument_keys)
        return {
            key: float((quote or {}).get("last_price", 0.0) or 0.0)
            for key, quote in quotes.items()
        }

    def place_order(self, request_body):
        token = getattr(request_body, "instrument_token", "")
        segment, security_id = self._parse_instrument_key(token)
        tag = str(getattr(request_body, "tag", "algo"))[:16]
        correlation_id = f"{tag}_{int(time.time() * 1000) % 10_000_000_000}"
        payload = {
            "dhanClientId": config.get_dhan_client_id(),
            "correlationId": correlation_id[:30],
            "transactionType": str(getattr(request_body, "transaction_type", "")).upper(),
            "exchangeSegment": segment,
            "productType": self._product_type(getattr(request_body, "product", "I")),
            "orderType": str(getattr(request_body, "order_type", "MARKET")).upper(),
            "validity": str(getattr(request_body, "validity", "DAY")).upper(),
            "securityId": str(security_id),
            "quantity": int(getattr(request_body, "quantity", 0) or 0),
            "disclosedQuantity": int(getattr(request_body, "disclosed_quantity", 0) or 0),
            "price": float(getattr(request_body, "price", 0.0) or 0.0),
            "triggerPrice": float(getattr(request_body, "trigger_price", 0.0) or 0.0),
            "afterMarketOrder": bool(getattr(request_body, "is_amo", False)),
            "amoTime": "",
            "boProfitValue": "",
            "boStopLossValue": "",
        }
        response = self._post("/orders", payload)
        order_id = response.get("orderId") or response.get("order_id")
        return SimpleNamespace(data=SimpleNamespace(order_ids=[order_id] if order_id else []))

    def get_order_status(self, order_id):
        response = self._get(f"/orders/{order_id}")
        status = str(response.get("orderStatus", "")).upper()
        mapped_status = "COMPLETE" if status == "TRADED" else status
        filled_qty = int(response.get("filledQty", 0) or 0)
        pending_qty = int(response.get("remainingQuantity", 0) or 0)
        return SimpleNamespace(data=SimpleNamespace(
            status=mapped_status,
            filled_quantity=filled_qty,
            pending_quantity=pending_qty,
            average_price=float(response.get("averageTradedPrice", 0.0) or 0.0),
            status_message=response.get("omsErrorDescription", "") or response.get("remarks", ""),
            status_message_raw=response.get("omsErrorCode", ""),
        ))

    def make_order_apis(self):
        return DhanOrderPlacementApi(self), DhanOrderStatusApi(self)

    def make_streamer(self, instrument_keys):
        return DhanPollingStreamer(self, instrument_keys)


_broker = None


def get_broker():
    global _broker
    if _broker is None:
        broker_name = config.get_active_broker()
        if broker_name == "UPSTOX":
            _broker = UpstoxBroker()
        elif broker_name == "DHAN":
            _broker = DhanBroker()
        else:
            logging.warning("Unknown BROKER=%s; falling back to Dhan adapter.", broker_name)
            _broker = DhanBroker()
    return _broker


def set_broker_for_tests(broker):
    global _broker
    _broker = broker
