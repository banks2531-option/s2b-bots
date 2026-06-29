"""Unusual Whales market-wide flow context. Phase 0: derive a coarse bias + an 'extreme' veto flag
from the market-tide endpoint; degrade to ('neutral', False) on ANY error so the bot never breaks.
Endpoint confirmed live 2026-06-29: GET /api/market/market-tide returns
  {"data": [{"net_call_premium": "<str>", "net_put_premium": "<str>", "net_volume": <int>, ...}]}
where net premium is SIGNED (positive = net bought, negative = net sold). `http` is injected for testing."""

EXTREME_NET_PREMIUM = 1.0e9   # |net call-minus-put premium| >= $1B = blow-out one-sided flow (veto candidate)


def parse_market_tide(payload) -> tuple:
    """UW market-tide payload -> (flow_bias, flow_extreme), using the most recent bucket.
    sentiment = net_call_premium - net_put_premium: calls bought (+) and/or puts sold (-) -> bullish;
    calls sold (-) and/or puts bought (+) -> bearish. extreme: |sentiment| >= EXTREME_NET_PREMIUM
    (a Phase-0 placeholder threshold to be calibrated against the logged data)."""
    rows = (payload if isinstance(payload, dict) else {}).get("data") or []
    if not rows:
        return ("neutral", False)
    r = rows[-1]                                   # most recent bucket
    call_p = float(r.get("net_call_premium") or 0)
    put_p = float(r.get("net_put_premium") or 0)
    sentiment = call_p - put_p
    if sentiment == 0:
        return ("neutral", False)
    bias = "bullish" if sentiment > 0 else "bearish"
    return (bias, abs(sentiment) >= EXTREME_NET_PREMIUM)


def flow_context(http, path="/api/market/market-tide") -> tuple:
    """Fetch + parse market tide. `http` is a callable (method, path)->json (UW REST).
    Returns ('neutral', False) on any failure — Phase 0 must never propagate a UW error."""
    try:
        return parse_market_tide(http("GET", path))
    except Exception:
        return ("neutral", False)
