"""SPY trend context from daily bars (same [{high,low,close}] format as feeds.compute_atr).
trend_bias compares the latest close to its 20- and 50-day SMAs; atr_pct = ATR(14)/close."""
from bot.app.feeds import compute_atr


def _sma(closes, n):
    w = closes[-n:]
    return sum(w) / len(w) if w else None


def trend_bias(bars, short_n=20, long_n=50, band=0.001) -> str:
    """'up' if close is above both SMAs, 'down' if below both, else 'neutral'. 'neutral' if too
    few bars. `band` ignores ties within 0.1% to avoid flip-flopping on flat tape."""
    closes = [float(b["close"]) for b in bars]
    if len(closes) < short_n:
        return "neutral"
    c = closes[-1]
    s = _sma(closes, short_n)
    l = _sma(closes, long_n) if len(closes) >= long_n else s
    if c > s * (1 + band) and c > l * (1 + band):
        return "up"
    if c < s * (1 - band) and c < l * (1 - band):
        return "down"
    return "neutral"


def atr_pct(bars, n=14):
    """ATR(n)/latest close as a fraction. None if no bars."""
    if not bars:
        return None
    atr = compute_atr(bars, n=n)
    c = float(bars[-1]["close"])
    return round(atr / c, 4) if c else None
