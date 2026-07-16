"""Allocated-equity + account-level SPY exposure supervisor (partner review v2 §2)."""
from collections import namedtuple

_ForeignSpread = namedtuple("_ForeignSpread", "short_strike long_strike credit qty expiry")


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


def foreign_spreads(all_broker_spy_spreads, own_positions):
    """QUANTITY-AWARE reconstruction of the foreign (not-opened-by-this-bot) SPY spreads at the
    broker (partner review v2 §2, Priority-0 fix item 6). A broker spread's (short_strike,
    long_strike, expiry) triple matching an own position no longer excludes it WHOLESALE -- only
    the quantity in excess of this bot's own tracked qty at that same key is foreign. E.g. broker
    holds 10 contracts of a spread this bot tracks 8 of -> 2 foreign contracts, not 0 and not 10.
    Returns a list of synthetic _ForeignSpread rows (one per broker spread with foreign qty > 0),
    each carrying .short_strike/.long_strike/.credit/.qty(=foreign qty)/.expiry, with credit
    defaulted to 0.0 (unknown/conservative) same as the broker-reconstructed spreads always were."""
    own_qty = {}
    for p in own_positions:
        k = (p.short_strike, p.long_strike, p.expiry)
        own_qty[k] = own_qty.get(k, 0) + p.qty
    # Aggregate broker-side rows by key (summing qty) BEFORE subtracting own_qty, so a duplicate
    # broker row at the same key can't double-subtract own_qty and undercount foreign. Not
    # exploitable today (the broker feed dedupes), but keeps this helper safe as a general utility.
    broker_qty = {}
    broker_credit = {}
    for b in all_broker_spy_spreads:
        k = (b.short_strike, b.long_strike, b.expiry)
        broker_qty[k] = broker_qty.get(k, 0) + b.qty
        broker_credit.setdefault(k, getattr(b, "credit", 0.0) or 0.0)
    out = []
    for k, bqty in broker_qty.items():
        fq = max(0, bqty - own_qty.get(k, 0))
        if fq > 0:
            short_strike, long_strike, expiry = k
            out.append(_ForeignSpread(short_strike, long_strike, broker_credit[k], fq, expiry))
    return out


def foreign_spy_exposure(all_broker_spy_spreads, own_positions):
    """Structural + stop dollar-risk of FOREIGN SPY spreads only -- broker-side spreads (or the
    broker-side EXCESS quantity of a spread this bot also holds) NOT opened by this bot (partner
    review v2 §2: foreign positions must still count toward this bot's TOTAL stop/structural
    budgets, but must never be double-counted against this bot's own open book). Quantity-aware
    (Priority-0 fix item 6): see foreign_spreads() for the matching logic. Reuses
    account_spy_exposure's per-spread math on the foreign subset, so foreign spreads still get the
    conservative full-width treatment when their credit is unknown (0.0), exactly as
    account_spy_exposure already does."""
    return account_spy_exposure(foreign_spreads(all_broker_spy_spreads, own_positions))
