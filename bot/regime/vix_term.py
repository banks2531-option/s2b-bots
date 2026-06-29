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


def fetch_vix_term(lookback=120):
    """(vix_level, vix_pct_rank, vix_term_slope) from yfinance ^VIX and ^VIX3M. Network glue;
    returns (None, None, None) on any failure so the engine degrades gracefully (no raise)."""
    try:
        import yfinance as yf
        def closes(sym):
            c = yf.download(sym, period="6mo", progress=False, auto_adjust=False)["Close"]
            if hasattr(c, "columns"):
                c = c.iloc[:, 0]
            return [float(x) for x in c.dropna().values]
        vix = closes("^VIX")
        v3m = closes("^VIX3M")
        if not vix:
            return (None, None, None)
        level = vix[-1]
        rank = pct_rank(level, vix[-lookback:])
        slope = term_slope(level, v3m[-1]) if v3m else 0.0
        return (round(level, 2), rank, slope)
    except Exception:
        return (None, None, None)
