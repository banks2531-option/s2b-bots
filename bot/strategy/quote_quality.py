"""Quote freshness and expected-move inputs for the low-credit-safety lane (advisor directive
Step 1; post-v2 refinement §1).

These are the two inputs the low_credit_safety gate was missing. Both FAIL CLOSED by design:

  * an age that cannot be determined is NOT fresh -- and the current time is never substituted for a
    missing quote timestamp, because that would make stale cached data read as perfectly fresh,
    which is precisely the failure this gate exists to catch;
  * an expected move that cannot be computed is NOT a pass -- the candidate must then qualify (if at
    all) through the short-delta branch of the OR.

PURE: no network, no clock. `now` is always injected by the caller."""
import math


MAX_QUOTE_AGE_SECONDS = 2.0        # spec §1 / advisor Step 1A: spread quote age ceiling
MIN_EXPECTED_MOVE_CUSHION = 0.90   # spec §1 / advisor Step 1B: strike cushion as a fraction of the
                                    # expected move to expiry


def calculate_quote_age_seconds(quote, now):
    """Seconds since this leg's quote was published, or None when that cannot be established.

    Prefers the exchange-provided timestamp and falls back to the local receive time. Clamped at 0 so
    clock skew between the exchange and this host cannot manufacture a negative (i.e. "from the
    future", trivially fresh) age."""
    timestamp = getattr(quote, "exchange_timestamp", None) or getattr(quote, "received_timestamp", None)
    if timestamp is None:
        return None
    # The live path's `now` is timezone-aware ET while the Tradier-derived quote time is also aware,
    # but tests and any hand-built quote use naive datetimes. Subtracting across the two raises
    # TypeError, so normalize to a common footing (both are ET wall-clock either way) rather than
    # letting an awareness mismatch crash the entry cycle.
    if (now.tzinfo is None) != (timestamp.tzinfo is None):
        now = now.replace(tzinfo=None)
        timestamp = timestamp.replace(tzinfo=None)
    return max(0.0, (now - timestamp).total_seconds())


def spread_quote_age_seconds(short_quote, long_quote, now):
    """Age of the OLDER leg -- a vertical is only as fresh as its stalest side. None when EITHER leg's
    age is unknown: taking the known leg's age would let an untimestamped stale leg ride through on
    its partner's freshness."""
    ages = [calculate_quote_age_seconds(short_quote, now), calculate_quote_age_seconds(long_quote, now)]
    if any(a is None for a in ages):
        return None
    return max(ages)


def quote_freshness_pass(spread_age_seconds) -> bool:
    """True only for a KNOWN age within the ceiling. Unknown -> False (fail closed)."""
    return spread_age_seconds is not None and spread_age_seconds <= MAX_QUOTE_AGE_SECONDS


def calculate_atm_iv(atm_call_iv, atm_put_iv):
    """Average the two at-the-money implied vols, use whichever side is valid if only one is, and
    return None when neither is (fail closed). Non-positive IVs are treated as absent."""
    valid = [iv for iv in (atm_call_iv, atm_put_iv) if iv is not None and iv > 0]
    if not valid:
        return None
    return sum(valid) / len(valid)


def expected_move_to_expiry(spot, annualized_atm_iv, calendar_days_to_expiry):
    """One-sigma expected move of the underlying by expiry: spot * IV * sqrt(days / 365).

    Returns None on any non-positive input rather than 0.0 -- a computed move of zero would make the
    cushion ratio infinite and wave every candidate through."""
    if spot is None or annualized_atm_iv is None or calendar_days_to_expiry is None:
        return None
    if spot <= 0 or annualized_atm_iv <= 0 or calendar_days_to_expiry <= 0:
        return None
    return spot * annualized_atm_iv * math.sqrt(calendar_days_to_expiry / 365.0)


def expected_move_cushion_of(*, spot, short_strike, expected_move):
    """How many expected moves of room sit between spot and the short strike (bull put spread).
    None when the expected move is unavailable or zero."""
    if expected_move is None or expected_move <= 0:
        return None
    return (spot - short_strike) / expected_move
