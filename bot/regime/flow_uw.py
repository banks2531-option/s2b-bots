"""Unusual Whales market-wide flow context. Phase 0: derive a coarse bias + an 'extreme' veto flag
from net option premium; degrade to ('neutral', False) on ANY error so the bot never breaks.
The exact endpoint is confirmed against the live key in wiring; `http` is injected for testing."""

EXTREME_RATIO = 10.0         # one side >= 10x the other -> blow-out flow (risk-off veto candidate)


def parse_market_tide(payload) -> tuple:
    """UW market-tide-shaped payload -> (flow_bias, flow_extreme).
    bias: 'bullish' if net call premium > net put premium, else 'bearish' (ties -> 'neutral').
    extreme: True when the dominant side is >= EXTREME_RATIO times the other."""
    rows = (payload if isinstance(payload, dict) else {}).get("data") or []
    if not rows:
        return ("neutral", False)
    r = rows[-1]                                   # most recent bucket
    call_p = float(r.get("net_call_premium") or 0)
    put_p = float(r.get("net_put_premium") or 0)
    if call_p == put_p:
        return ("neutral", False)
    bias = "bullish" if call_p > put_p else "bearish"
    hi, lo = max(call_p, put_p), max(min(call_p, put_p), 1.0)
    return (bias, hi / lo >= EXTREME_RATIO)


def flow_context(http, path="/api/market/market-tide") -> tuple:
    """Fetch + parse market tide. `http` is a callable (method, path)->json (UW REST).
    Returns ('neutral', False) on any failure — Phase 0 must never propagate a UW error."""
    try:
        return parse_market_tide(http("GET", path))
    except Exception:
        return ("neutral", False)
