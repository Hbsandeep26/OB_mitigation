import datetime
from dataclasses import dataclass

import config


SENTIMENT_EXTREMELY_BULLISH = "EXTREMELY_BULLISH"
SENTIMENT_MILDLY_BULLISH = "MILDLY_BULLISH"
SENTIMENT_NEUTRAL = "NEUTRAL"
SENTIMENT_MILDLY_BEARISH = "MILDLY_BEARISH"
SENTIMENT_EXTREMELY_BEARISH = "EXTREMELY_BEARISH"
SENTIMENT_UNKNOWN = "UNKNOWN"

FLOW_SIGNAL_BULLISH = "BULLISH"
FLOW_SIGNAL_BEARISH = "BEARISH"
FLOW_SIGNAL_NEUTRAL = "NEUTRAL"
FLOW_SIGNAL_CONFLICTED = "CONFLICTED"
FLOW_SIGNAL_UNKNOWN = "UNKNOWN"

STRADDLE_EXPANDING = "EXPANDING"
STRADDLE_CONTRACTING = "CONTRACTING"
STRADDLE_FLAT = "FLAT"
STRADDLE_UNKNOWN = "UNKNOWN"


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


@dataclass
class OiFlowContext:
    index_symbol: str
    expiry_date: str
    dte: int
    spot: float = None
    atm_strike: float = None
    flow_signal: str = FLOW_SIGNAL_UNKNOWN
    straddle_signal: str = STRADDLE_UNKNOWN
    straddle_premium: float = None
    previous_straddle_premium: float = None
    straddle_change_pct: float = None
    sentiment: str = SENTIMENT_UNKNOWN
    oi_flow_snapshot: dict = None
    flow_metrics: dict = None
    no_trade_reason: str = ""

    def as_dict(self):
        return {
            "index_symbol": self.index_symbol,
            "expiry_date": self.expiry_date,
            "dte": self.dte,
            "spot": self.spot,
            "atm_strike": self.atm_strike,
            "flow_signal": self.flow_signal,
            "straddle_signal": self.straddle_signal,
            "straddle_premium": self.straddle_premium,
            "previous_straddle_premium": self.previous_straddle_premium,
            "straddle_change_pct": self.straddle_change_pct,
            "sentiment": self.sentiment,
            "oi_flow_snapshot": self.oi_flow_snapshot or {},
            "flow_metrics": self.flow_metrics or {},
            "no_trade_reason": self.no_trade_reason,
            "pcr": None,
            "vix": None,
        }


def _float_or_none(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _float_any(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _option_oi(strike_data, option_key):
    option_data = strike_data.get(option_key, {}) or {}
    market_data = option_data.get("market_data", {}) or {}
    return _float_or_none(market_data.get("oi")) or 0.0


def _option_market_data(strike_data, option_key):
    option_data = strike_data.get(option_key, {}) or {}
    return option_data.get("market_data", {}) or {}


def _option_ltp(strike_data, option_key):
    return _float_or_none(_option_market_data(strike_data, option_key).get("ltp")) or 0.0


def _option_oi_delta(strike_data, option_key):
    option_data = strike_data.get(option_key, {}) or {}
    market_data = option_data.get("market_data", {}) or {}
    for source in (market_data, option_data):
        for key in (
            "change_oi",
            "change_in_oi",
            "oi_change",
            "open_interest_change",
            "oi_day_change",
            "oiChange",
            "changeOI",
            "changeInOI",
        ):
            value = _float_any(source.get(key))
            if value is not None:
                return value
    return None


def _sorted_strikes(option_chain_data):
    strikes = []
    for strike_data in option_chain_data or []:
        value = _float_any(strike_data.get("strike_price"))
        if value is not None:
            strikes.append(value)
    return sorted(set(strikes))


def _nearest_strike(option_chain_data, spot):
    strikes = _sorted_strikes(option_chain_data)
    if not strikes or spot is None:
        return None
    return min(strikes, key=lambda strike: abs(strike - float(spot)))


def _strike_row(option_chain_data, strike):
    target = _float_any(strike)
    for strike_data in option_chain_data or []:
        current = _float_any(strike_data.get("strike_price"))
        if current == target:
            return strike_data
    return {}


def _band_strikes(option_chain_data, atm_strike, width=3):
    strikes = _sorted_strikes(option_chain_data)
    if atm_strike is None or atm_strike not in strikes:
        return []
    atm_idx = strikes.index(atm_strike)
    start = max(0, atm_idx - int(width))
    end = min(len(strikes), atm_idx + int(width) + 1)
    return strikes[start:end]


def _previous_band_entry(previous_snapshot, strike):
    band = (previous_snapshot or {}).get("band", {}) or {}
    return band.get(str(strike)) or band.get(str(float(strike))) or {}


def _snapshot_from_band(atm_strike, straddle_premium, band_rows):
    return {
        "atm_strike": atm_strike,
        "straddle_premium": round(float(straddle_premium or 0.0), 4),
        "band": {
            str(row["strike"]): {
                "call_oi": round(row["call_oi"], 4),
                "put_oi": round(row["put_oi"], 4),
                "call_ltp": round(row["call_ltp"], 4),
                "put_ltp": round(row["put_ltp"], 4),
            }
            for row in band_rows
        },
    }


def _dominates(primary, secondary, min_change):
    dominance = max(float(config.OI_FLOW_DOMINANCE_RATIO), 1.0)
    return primary >= min_change and primary >= (secondary * dominance)


def classify_oi_flow(band_rows, previous_snapshot=None):
    previous_band = (previous_snapshot or {}).get("band", {}) or {}
    previous_total_oi = 0.0
    for item in previous_band.values():
        previous_total_oi += float(item.get("call_oi", 0.0) or 0.0)
        previous_total_oi += float(item.get("put_oi", 0.0) or 0.0)

    current_total_oi = sum(row["call_oi"] + row["put_oi"] for row in band_rows)
    threshold_basis = previous_total_oi if previous_total_oi > 0 else current_total_oi
    min_change = max(
        float(config.OI_FLOW_MIN_ABS_CHANGE),
        threshold_basis * float(config.OI_FLOW_MIN_BAND_CHANGE_PCT),
    )

    deltas = [
        delta
        for row in band_rows
        for delta in (row.get("call_delta"), row.get("put_delta"))
        if delta is not None
    ]
    if not deltas:
        return FLOW_SIGNAL_UNKNOWN, {
            "reason": "No OI delta available yet",
            "min_change": round(min_change, 4),
            "current_total_oi": round(current_total_oi, 4),
            "previous_total_oi": round(previous_total_oi, 4),
        }

    call_writing = sum(max(float(row.get("call_delta") or 0.0), 0.0) for row in band_rows)
    put_writing = sum(max(float(row.get("put_delta") or 0.0), 0.0) for row in band_rows)
    call_unwinding = sum(abs(min(float(row.get("call_delta") or 0.0), 0.0)) for row in band_rows)
    put_unwinding = sum(abs(min(float(row.get("put_delta") or 0.0), 0.0)) for row in band_rows)

    bullish_score = call_unwinding + put_writing
    bearish_score = put_unwinding + call_writing
    bullish = (
        call_unwinding >= min_change
        and put_writing >= min_change
        and _dominates(bullish_score, bearish_score, min_change)
    )
    bearish = (
        put_unwinding >= min_change
        and call_writing >= min_change
        and _dominates(bearish_score, bullish_score, min_change)
    )
    neutral = (
        call_writing >= min_change
        and put_writing >= min_change
        and not bullish
        and not bearish
    )

    metrics = {
        "min_change": round(min_change, 4),
        "current_total_oi": round(current_total_oi, 4),
        "previous_total_oi": round(previous_total_oi, 4),
        "call_writing": round(call_writing, 4),
        "put_writing": round(put_writing, 4),
        "call_unwinding": round(call_unwinding, 4),
        "put_unwinding": round(put_unwinding, 4),
        "bullish_score": round(bullish_score, 4),
        "bearish_score": round(bearish_score, 4),
    }

    if bullish and bearish:
        return FLOW_SIGNAL_CONFLICTED, metrics
    if bullish:
        return FLOW_SIGNAL_BULLISH, metrics
    if bearish:
        return FLOW_SIGNAL_BEARISH, metrics
    if neutral:
        return FLOW_SIGNAL_NEUTRAL, metrics
    return FLOW_SIGNAL_CONFLICTED, metrics


def classify_straddle_premium(current_premium, previous_premium):
    current = _float_or_none(current_premium)
    previous = _float_or_none(previous_premium)
    if current is None or previous is None:
        return STRADDLE_UNKNOWN, None

    change_pct = (current - previous) / previous
    threshold = float(config.STRADDLE_PREMIUM_CHANGE_PCT)
    if change_pct >= threshold:
        return STRADDLE_EXPANDING, change_pct
    if change_pct <= -threshold:
        return STRADDLE_CONTRACTING, change_pct
    return STRADDLE_FLAT, change_pct


def _sentiment_from_flow(flow_signal):
    if flow_signal == FLOW_SIGNAL_BULLISH:
        return SENTIMENT_EXTREMELY_BULLISH
    if flow_signal == FLOW_SIGNAL_BEARISH:
        return SENTIMENT_EXTREMELY_BEARISH
    if flow_signal == FLOW_SIGNAL_NEUTRAL:
        return SENTIMENT_NEUTRAL
    return SENTIMENT_UNKNOWN


def build_oi_flow_context(index_symbol, expiry_date, option_chain, spot=None, previous_snapshot=None, now=None):
    dte = days_to_expiry(expiry_date, now=now)
    if spot is None:
        for strike_data in option_chain or []:
            spot = _float_or_none(strike_data.get("underlying_spot_price"))
            if spot is not None:
                break

    atm_strike = _nearest_strike(option_chain, spot)
    if atm_strike is None:
        return OiFlowContext(
            index_symbol=index_symbol,
            expiry_date=str(expiry_date),
            dte=dte,
            spot=_float_or_none(spot),
            no_trade_reason="Option chain has no ATM strike",
        )

    band_rows = []
    for strike in _band_strikes(option_chain, atm_strike, width=3):
        row = _strike_row(option_chain, strike)
        previous = _previous_band_entry(previous_snapshot, strike)
        call_oi = _option_oi(row, "call_options")
        put_oi = _option_oi(row, "put_options")
        call_delta = _option_oi_delta(row, "call_options")
        put_delta = _option_oi_delta(row, "put_options")
        if call_delta is None and previous:
            call_delta = call_oi - float(previous.get("call_oi", 0.0) or 0.0)
        if put_delta is None and previous:
            put_delta = put_oi - float(previous.get("put_oi", 0.0) or 0.0)
        band_rows.append({
            "strike": strike,
            "call_oi": call_oi,
            "put_oi": put_oi,
            "call_ltp": _option_ltp(row, "call_options"),
            "put_ltp": _option_ltp(row, "put_options"),
            "call_delta": call_delta,
            "put_delta": put_delta,
        })

    atm_row = _strike_row(option_chain, atm_strike)
    straddle_premium = _option_ltp(atm_row, "call_options") + _option_ltp(atm_row, "put_options")
    previous_straddle = (previous_snapshot or {}).get("straddle_premium")
    straddle_signal, straddle_change_pct = classify_straddle_premium(straddle_premium, previous_straddle)
    snapshot = _snapshot_from_band(atm_strike, straddle_premium, band_rows)
    flow_signal, flow_metrics = classify_oi_flow(band_rows, previous_snapshot=previous_snapshot)

    no_trade_reason = ""
    if flow_signal == FLOW_SIGNAL_UNKNOWN:
        no_trade_reason = flow_metrics.get("reason", "OI flow unavailable")
    elif straddle_signal == STRADDLE_UNKNOWN:
        no_trade_reason = "Straddle premium baseline unavailable"
    elif straddle_signal == STRADDLE_FLAT:
        no_trade_reason = "Straddle premium unchanged"
    elif flow_signal == FLOW_SIGNAL_CONFLICTED:
        no_trade_reason = "OI flow conflicted"

    return OiFlowContext(
        index_symbol=index_symbol,
        expiry_date=str(expiry_date),
        dte=dte,
        spot=_float_or_none(spot),
        atm_strike=atm_strike,
        flow_signal=flow_signal,
        straddle_signal=straddle_signal,
        straddle_premium=round(straddle_premium, 4),
        previous_straddle_premium=round(float(previous_straddle), 4) if previous_straddle else None,
        straddle_change_pct=round(straddle_change_pct, 6) if straddle_change_pct is not None else None,
        sentiment=_sentiment_from_flow(flow_signal),
        oi_flow_snapshot=snapshot,
        flow_metrics=flow_metrics,
        no_trade_reason=no_trade_reason,
    )


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
