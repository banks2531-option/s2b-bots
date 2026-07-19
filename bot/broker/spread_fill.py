"""Leg-level normalization of a Tradier multileg order fill (advisor nextsteps2 section 1).

WHY THIS EXISTS. The order-level fields are not usable against the LIVE Tradier broker:

  * `avg_fill_price` is NEGATIVE for a credit spread (-0.92 means $0.92 RECEIVED). Recording
    `credit = avg_fill_price` flipped the sign, which produced a negative stop threshold, a
    phantom STOP, ~150 rejected stop-closes and a fake -$367 unrealized P&L on the live account.
  * `exec_quantity` counts LEGS, not contracts: a 1-contract vertical reports 2.0.

Neither quirk reproduces on the sandbox, which omits the leg array entirely -- which is exactly
how the bug reached production. The fix is NOT to negate and halve the order-level numbers; a
sign convention we inferred from one incident is not a contract. We reconstruct the economics
from the individual legs, which are unambiguous:

    opening_credit  = short_put_sell_fill  - long_put_buy_fill
    closing_debit   = short_put_buy_fill   - long_put_sell_fill
    contracts       = min(short_leg_exec_qty, long_leg_exec_qty)

`min()` rather than `exec_quantity / 2` because unequal legs are a real, dangerous state: three
short puts against two long puts is two covered spreads plus one NAKED short, not three spreads.
We report the covered count and set `balanced=False` so the caller halts and reconciles. We never
invent a complete spread.

FAILS CLOSED. Any payload this module cannot read with certainty -- no leg array, not two legs,
a missing fill price, a nonsensical negative net -- returns None, and the caller falls back to
the pre-existing order-level behaviour. That keeps Bot B's sandbox accounting byte-identical.
"""
from dataclasses import dataclass

# Tradier leg sides. On a bull put spread the SHORT leg is the higher-strike put we sold to open
# and buy back to close; the LONG leg is the lower-strike protective put.
_SHORT_SIDES = {"sell_to_open", "buy_to_close"}
_LONG_SIDES = {"buy_to_open", "sell_to_close"}
# Sides that mean the position is being ESTABLISHED (cash in) vs CLOSED (cash out).
_OPENING_SIDES = {"sell_to_open", "buy_to_open"}


@dataclass
class NormalizedSpreadFill:
    """One multileg spread execution, reconstructed from its legs.

    `net_price` is always a POSITIVE magnitude in dollars-per-share; `cash_flow_type` carries the
    direction. Keeping sign out of the number is deliberate: the live incident was a sign
    convention silently disagreeing between the broker and our accounting."""
    contracts: int
    net_price: float
    cash_flow_type: str          # "credit" (opening) | "debit" (closing)

    short_leg_qty: int
    long_leg_qty: int

    short_leg_price: float
    long_leg_price: float

    balanced: bool
    source_order_id: str | None


def _leg_float(leg, field):
    v = leg.get(field)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def normalize_spread_fill(order):
    """Reconstruct spread economics from a Tradier order's leg array.

    Returns a NormalizedSpreadFill, or None when the payload cannot be read with certainty (no
    leg array, not exactly one short + one long leg, or a missing/unparseable fill price). None
    means "fall back to the caller's existing behaviour", never "no fill"."""
    if not isinstance(order, dict):
        return None
    legs = order.get("leg")
    if not isinstance(legs, list) or len(legs) != 2:
        return None

    short_leg = long_leg = None
    for leg in legs:
        if not isinstance(leg, dict):
            return None
        side = (leg.get("side") or "").lower()
        if side in _SHORT_SIDES and short_leg is None:
            short_leg = leg
        elif side in _LONG_SIDES and long_leg is None:
            long_leg = leg
    if short_leg is None or long_leg is None:
        return None

    # NOTE: exec_quantity (what FILLED), never quantity (what was ORDERED). A partially-filled
    # leg reports the smaller exec_quantity, and booking the ordered amount is precisely the
    # untracked-at-broker bug this module exists to prevent.
    short_qty = _leg_float(short_leg, "exec_quantity")
    long_qty = _leg_float(long_leg, "exec_quantity")
    if short_qty is None or long_qty is None:
        return None
    # Options fill in whole contracts. A fractional quantity is a broker data fault, and int()
    # below would truncate it toward zero -- silently losing a contract from the recorded
    # position while the broker still holds it. Every other unreadable input here returns None;
    # this one must too, or the module's fail-closed contract has a hole in exactly the place
    # that produces an under-recorded live position.
    if short_qty != int(short_qty) or long_qty != int(long_qty):
        return None

    short_px = _leg_float(short_leg, "avg_fill_price")
    long_px = _leg_float(long_leg, "avg_fill_price")
    # A zero-fill order legitimately has no prices; report the zero rather than failing closed,
    # so a "nothing filled" outcome is distinguishable from an unreadable payload.
    if short_qty == 0 and long_qty == 0:
        short_px = short_px if short_px is not None else 0.0
        long_px = long_px if long_px is not None else 0.0
    if short_px is None or long_px is None:
        return None

    net = short_px - long_px
    if net < 0:
        # The short leg of a bull put spread is always the more valuable one. A negative net means
        # the legs were misidentified or the payload is malformed. Refuse it rather than record a
        # number we cannot explain.
        return None

    opening = (short_leg.get("side") or "").lower() in _OPENING_SIDES
    oid = order.get("id")
    return NormalizedSpreadFill(
        contracts=int(min(short_qty, long_qty)),
        net_price=net,
        cash_flow_type="credit" if opening else "debit",
        short_leg_qty=int(short_qty),
        long_leg_qty=int(long_qty),
        short_leg_price=short_px,
        long_leg_price=long_px,
        balanced=(short_qty == long_qty),
        source_order_id=(str(oid) if oid is not None else None),
    )
