"""Production data adapters: pure parsers/calculators injected into Deps (spec §3,§5,§7)."""
from datetime import datetime, timedelta


def pick_weekly_expiry(today: str, min_dte: int = 4) -> str:
    """Nearest Friday at least min_dte calendar days out (YYYY-MM-DD)."""
    d = datetime.strptime(today, "%Y-%m-%d")
    days_to_fri = (4 - d.weekday()) % 7        # 4 = Friday
    friday = d + timedelta(days=days_to_fri)
    while (friday - d).days < min_dte:
        friday += timedelta(days=7)
    return friday.strftime("%Y-%m-%d")


from bot.strategy.s2b import OptionQuote


def parse_chain(resp) -> list:
    """Tradier options-chain JSON -> [OptionQuote] for PUTS with a usable delta (abs)."""
    options = (resp.get("options") or {}).get("option") or []
    if isinstance(options, dict):
        options = [options]
    out = []
    for o in options:
        if o.get("option_type") != "put":
            continue
        delta = (o.get("greeks") or {}).get("delta")
        if delta is None:
            continue
        bid = o.get("bid")
        ask = o.get("ask")
        if bid is None or ask is None:
            continue
        out.append(OptionQuote(strike=float(o["strike"]), delta=abs(float(delta)),
                               bid=float(bid), ask=float(ask)))
    return out


def parse_equity(resp) -> float:
    return float(resp["balances"]["total_equity"])


def parse_position_legs(resp) -> dict:
    """Tradier positions JSON -> {occ_symbol: int qty}. Handles 'null' and single-object cases."""
    positions = (resp.get("positions") or {})
    if positions in (None, "null"):
        return {}
    items = positions.get("position")
    if not items:
        return {}
    if isinstance(items, dict):       # Tradier returns a bare object for a single position
        items = [items]
    return {p["symbol"]: int(p["quantity"]) for p in items}


def compute_atr(bars, n: int = 14) -> float:
    """ATR over the last n bars. bars: list of {high, low, close} oldest->newest."""
    trs = []
    prev_close = None
    for b in bars:
        h, l, c = float(b["high"]), float(b["low"]), float(b["close"])
        tr = h - l if prev_close is None else max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)
        prev_close = c
    window = trs[-n:]
    if not window:
        raise ValueError("compute_atr: no bars")
    return round(sum(window) / len(window), 4)


def vix_regime(vix_series):
    """Return (pct_rank, 1-day change) of the latest VIX vs the series. pct_rank in [0,1]."""
    s = list(vix_series)
    latest = s[-1]
    pct_rank = sum(1 for x in s if x <= latest) / len(s)
    change = (latest / s[-2] - 1.0) if len(s) >= 2 and s[-2] else 0.0
    return round(pct_rank, 4), round(change, 4)


import re as _re


def parse_occ(occ_symbol: str):
    """Parse an OCC option symbol -> (ticker, expiry_YYYY-MM-DD, right, strike_float).
    E.g. 'SPY260619P00568000' -> ('SPY', '2026-06-19', 'P', 568.0)
    """
    m = _re.match(r'^([A-Z]+)(\d{6})([CP])(\d{8})$', occ_symbol)
    if not m:
        raise ValueError(f"Cannot parse OCC symbol: {occ_symbol}")
    ticker, yymmdd, right, strike_str = m.groups()
    expiry = datetime.strptime(yymmdd, "%y%m%d").strftime("%Y-%m-%d")
    strike = int(strike_str) / 1000.0
    return (ticker, expiry, right, strike)


from bot.strategy.manage import ManagedPosition as _ManagedPosition


def reconstruct_spreads(leg_map: dict) -> list:
    """Reconstruct bull put spreads from a {occ_symbol: signed_qty} leg map.
    Pairs each short leg (qty < 0) with the NEAREST long leg below it (same ticker/expiry,
    matching qty) and consumes that long so it can't be reused. Processing shorts high-strike
    first means adjacent/overlapping spreads (e.g. 721/711 + 720/710) reconstruct correctly
    instead of both shorts grabbing the lowest long (which orphaned a leg and faked a "missing"
    position -> spurious reconcile halt). Returns ManagedPosition with credit=0.0 (unknown from broker).
    """
    shorts = {}  # (ticker, expiry, strike) -> qty (positive count)
    longs = {}   # (ticker, expiry, strike) -> qty
    for sym, qty in leg_map.items():
        if qty == 0:
            continue
        try:
            ticker, expiry, right, strike = parse_occ(sym)
        except ValueError:
            continue
        if right != "P":
            continue
        key = (ticker, expiry, strike)
        if qty < 0:
            shorts[key] = abs(qty)
        else:
            longs[key] = qty

    result = []
    # Highest short strike first; pair with the nearest (highest) long strictly below it, then
    # remove that long from the pool so a later short can't reuse it.
    for (ticker, expiry, short_strike), short_qty in sorted(shorts.items(), key=lambda kv: -kv[0][2]):
        candidates = [(ls, lt, le) for (lt, le, ls), lq in longs.items()
                      if lt == ticker and le == expiry and ls < short_strike and lq == short_qty]
        if not candidates:
            continue
        long_strike, lt, le = max(candidates)        # nearest long below the short
        del longs[(lt, le, long_strike)]             # consume it
        result.append(_ManagedPosition(
            ticker=ticker,
            short_strike=short_strike,
            long_strike=long_strike,
            credit=0.0,
            qty=short_qty,
            expiry=expiry,
        ))
    return result


def is_market_hours(now) -> bool:
    """True if `now` (ET datetime) is a weekday within 09:30-15:59 ET. Used to no-op the bot
    off-hours so it never marks positions on null quotes (which would halt it overnight).
    NOTE: does not account for market holidays (rare; would surface as a transient halt)."""
    if now.weekday() >= 5:                      # Sat/Sun
        return False
    minutes = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= minutes < (16 * 60)


def parse_expirations(resp) -> list:
    """Tradier /markets/options/expirations JSON -> [YYYY-MM-DD] (handles single/none)."""
    exp = (resp.get("expirations") or {})
    if exp in (None, "null"):
        return []
    dates = exp.get("date")
    if not dates:
        return []
    if isinstance(dates, str):
        dates = [dates]
    return list(dates)


def pick_expiry_from_list(available, today: str, min_dte: int = 4):
    """Broker-aware weekly expiry: nearest Friday >= min_dte that the broker actually lists;
    if that week's Friday is a market holiday (absent), fall back to the latest available
    trading day in that Mon-Fri week (e.g. the Thursday). None if nothing fits."""
    avail = set(available)
    d0 = datetime.strptime(today, "%Y-%m-%d")
    days_to_fri = (4 - d0.weekday()) % 7
    friday = d0 + timedelta(days=days_to_fri)
    while (friday - d0).days < min_dte:
        friday += timedelta(days=7)
    for _ in range(8):                                  # scan up to 8 weeks out
        fstr = friday.strftime("%Y-%m-%d")
        if fstr in avail:
            return fstr
        for k in range(1, 5):                           # holiday Friday -> Thu, Wed, ... of that week
            wd = friday - timedelta(days=k)
            wstr = wd.strftime("%Y-%m-%d")
            if wstr in avail and (wd - d0).days >= min_dte:
                return wstr
        friday += timedelta(days=7)
    return None


def parse_history(resp) -> list:
    """Tradier /markets/history daily JSON -> [{high, low, close}] oldest->newest (for compute_atr)."""
    hist = (resp.get("history") or {})
    if hist in (None, "null"):
        return []
    items = hist.get("day")
    if not items:
        return []
    if isinstance(items, dict):          # single-day responses come back as a bare object
        items = [items]
    return [{"high": float(d["high"]), "low": float(d["low"]), "close": float(d["close"])}
            for d in items]
