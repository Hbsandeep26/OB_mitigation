import datetime
from dataclasses import dataclass


SENTIMENT_EXTREMELY_BULLISH = "EXTREMELY_BULLISH"
SENTIMENT_MILDLY_BULLISH = "MILDLY_BULLISH"
SENTIMENT_NEUTRAL = "NEUTRAL"
SENTIMENT_MILDLY_BEARISH = "MILDLY_BEARISH"
SENTIMENT_EXTREMELY_BEARISH = "EXTREMELY_BEARISH"
SENTIMENT_UNKNOWN = "UNKNOWN"


@dataclass
class MarketContext:
    index_symbol: str
    expiry_date: str
    dte: int
    pcr: float = None
    vix: float = None
    spot: float = None
    sentiment: str = SENTIMENT_UNKNOWN

    def as_dict(self):
        return {
            "index_symbol": self.index_symbol,
            "expiry_date": self.expiry_date,
            "dte": self.dte,
            "pcr": self.pcr,
            "vix": self.vix,
            "spot": self.spot,
            "sentiment": self.sentiment,
        }


def _float_or_none(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _option_oi(strike_data, option_key):
    option_data = strike_data.get(option_key, {}) or {}
    market_data = option_data.get("market_data", {}) or {}
    return _float_or_none(market_data.get("oi")) or 0.0


def calculate_chain_pcr(option_chain_data):
    total_call_oi = 0.0
    total_put_oi = 0.0
    fallback_values = []

    for strike_data in option_chain_data or []:
        total_call_oi += _option_oi(strike_data, "call_options")
        total_put_oi += _option_oi(strike_data, "put_options")
        pcr_value = _float_or_none(strike_data.get("pcr"))
        if pcr_value is not None:
            fallback_values.append(pcr_value)

    if total_call_oi > 0 and total_put_oi > 0:
        return total_put_oi / total_call_oi

    if fallback_values:
        return sum(fallback_values) / len(fallback_values)

    return None


def days_to_expiry(expiry_date, now=None):
    now = now or datetime.datetime.now()
    try:
        expiry = datetime.datetime.strptime(str(expiry_date), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return 0
    return max(0, (expiry - now.date()).days)


def classify_pcr_sentiment(pcr, dte):
    if pcr is None:
        return SENTIMENT_UNKNOWN

    pcr = float(pcr)
    if int(dte) <= 3:
        if pcr > 1.30:
            return SENTIMENT_EXTREMELY_BULLISH
        if pcr >= 1.00:
            return SENTIMENT_MILDLY_BULLISH
        if pcr >= 0.80:
            return SENTIMENT_NEUTRAL
        if pcr >= 0.50:
            return SENTIMENT_MILDLY_BEARISH
        return SENTIMENT_EXTREMELY_BEARISH

    if pcr > 1.15:
        return SENTIMENT_EXTREMELY_BULLISH
    if pcr >= 0.95:
        return SENTIMENT_MILDLY_BULLISH
    if pcr >= 0.75:
        return SENTIMENT_NEUTRAL
    if pcr >= 0.60:
        return SENTIMENT_MILDLY_BEARISH
    return SENTIMENT_EXTREMELY_BEARISH


def build_market_context(index_symbol, expiry_date, option_chain, india_vix, now=None, spot=None):
    dte = days_to_expiry(expiry_date, now=now)
    pcr = calculate_chain_pcr(option_chain)

    if spot is None:
        for strike_data in option_chain or []:
            spot = _float_or_none(strike_data.get("underlying_spot_price"))
            if spot is not None:
                break

    context = MarketContext(
        index_symbol=index_symbol,
        expiry_date=str(expiry_date),
        dte=dte,
        pcr=round(pcr, 6) if pcr is not None else None,
        vix=_float_or_none(india_vix),
        spot=_float_or_none(spot),
    )
    context.sentiment = classify_pcr_sentiment(context.pcr, context.dte)
    return context
