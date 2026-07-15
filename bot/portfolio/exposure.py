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


def foreign_spy_exposure(all_broker_spy_spreads, own_positions):
    """Structural + stop dollar-risk of FOREIGN SPY spreads only -- broker-side spreads NOT opened
    by this bot (partner review v2 §2: foreign positions must still count toward this bot's TOTAL
    stop/structural budgets, but must never be double-counted against this bot's own open book).
    A broker spread matches an own position (and is excluded) when its (short_strike, long_strike,
    expiry) triple matches one of own_positions' -- the same identity the rest of the book uses
    (see bot.ops.ledger.position_key). Reuses account_spy_exposure's per-spread math on the
    remaining (foreign) subset, so foreign spreads still get the conservative full-width treatment
    when their credit is unknown (0.0), exactly as account_spy_exposure already does."""
    own_keys = {(p.short_strike, p.long_strike, p.expiry) for p in own_positions}
    foreign = [p for p in all_broker_spy_spreads
               if (p.short_strike, p.long_strike, p.expiry) not in own_keys]
    return account_spy_exposure(foreign)
