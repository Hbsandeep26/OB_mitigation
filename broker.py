import logging
import urllib.parse

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


_broker = None


def get_broker():
    global _broker
    if _broker is None:
        broker_name = str(config._setting("BROKER", "UPSTOX")).upper()
        if broker_name != "UPSTOX":
            logging.warning("Unknown BROKER=%s; falling back to Upstox adapter.", broker_name)
        _broker = UpstoxBroker()
    return _broker


def set_broker_for_tests(broker):
    global _broker
    _broker = broker
