"""VIX term-structure context. term_slope < 0 (front > 3M, backwardation) signals stress.
Pure calcs are unit-tested; fetch_vix_term() is thin network glue (yfinance) used in wiring."""


def term_slope(vix_front: float, vix_3m: float) -> float:
    """(3M / front) - 1. Negative = backwardation (front > 3M = stress). 0.0 if 3M is missing/zero."""
    if not vix_3m or not vix_front:
        return 0.0
    return round(vix_3m / vix_front - 1.0, 4)


def pct_rank(latest: float, series) -> float:
    """Fraction of `series` <= latest, in [0,1]. None if series is empty (unknown)."""
    s = list(series)
    if not s:
        return None
    return round(sum(1 for x in s if x <= latest) / len(s), 4)


import time as _time
from bot.regime.timeoututil import run_with_timeout

_TERM_CACHE = {"ts": 0.0, "val": (None, None, None)}
_TERM_TTL = 900          # refetch VIX term at most every 15 min (VIX is meaningless at 60s granularity)
_TERM_TIMEOUT = 20       # bound a yfinance network HANG (it has no timeout of its own)


def _download_term(lookback):
    """The raw yfinance fetch -> (level, pct_rank, slope). Raises on no data so the caller can fall
    back to the last cached value rather than blanking the signal."""
    import yfinance as yf
    def closes(sym):
        c = yf.download(sym, period="6mo", progress=False, auto_adjust=False)["Close"]
        if hasattr(c, "columns"):
            c = c.iloc[:, 0]
        return [float(x) for x in c.dropna().values]
    vix = closes("^VIX")
    if not vix:
        raise ValueError("no VIX data")
    v3m = closes("^VIX3M")
    level = vix[-1]
    rank = pct_rank(level, vix[-lookback:])
    slope = term_slope(level, v3m[-1]) if v3m else 0.0
    return (round(level, 2), rank, slope)


def fetch_vix_term(lookback=120):
    """(vix_level, vix_pct_rank, vix_term_slope) from yfinance ^VIX/^VIX3M, CACHED for _TERM_TTL and
    HANG-bounded by _TERM_TIMEOUT. Returns the last good value on timeout/error (or (None,None,None)
    before any success) so the engine degrades gracefully and the tick loop can never stall here."""
    now = _time.time()
    if (now - _TERM_CACHE["ts"]) < _TERM_TTL and _TERM_CACHE["val"][0] is not None:
        return _TERM_CACHE["val"]
    val = run_with_timeout(lambda: _download_term(lookback), _TERM_TIMEOUT, _TERM_CACHE["val"])
    if val and val[0] is not None:
        _TERM_CACHE["ts"], _TERM_CACHE["val"] = now, val
    return val
