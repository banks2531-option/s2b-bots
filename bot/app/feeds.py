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
    out = []
    for o in options:
        if o.get("option_type") != "put":
            continue
        delta = (o.get("greeks") or {}).get("delta")
        if delta is None:
            continue
        out.append(OptionQuote(strike=float(o["strike"]), delta=abs(float(delta)),
                               bid=float(o["bid"]), ask=float(o["ask"])))
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
