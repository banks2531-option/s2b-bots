"""Allocated-equity + account-level SPY exposure supervisor (partner review v2 §2)."""

def risk_equity(allocated_equity, broker_equity):
    """The equity this bot sizes risk against: min(allocated, broker). Never exceed the allocation."""
    return min(allocated_equity, broker_equity)

def account_spy_exposure(spy_spreads):
    """Aggregate structural + stop dollar-risk across EVERY SPY spread at the broker (incl. foreign
    positions not opened by this bot). Each spread: has .short_strike, .long_strike, .credit, .qty.
    Foreign positions reconstructed from the broker have credit=0.0 (unknown), so their structural
    risk uses the full wing width and their stop risk is treated as the full wing (conservative)."""
    wing = 10.0
    total_structural = 0.0
    total_stop = 0.0
    for p in spy_spreads:
        width = abs(p.short_strike - p.long_strike) or wing
        credit = getattr(p, "credit", 0.0) or 0.0
        total_structural += (width - credit) * 100 * p.qty
        # stop = 2x credit loss; foreign (credit 0) -> conservative full-width
        total_stop += (2 * credit * 100 * p.qty) if credit > 0 else (width * 100 * p.qty)
    return {"structural": round(total_structural, 2), "stop": round(total_stop, 2)}
