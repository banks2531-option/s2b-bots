"""Credit-quality tiering (partner review v2 §3): size by credit-as-%-of-wing, adaptive per DTE
bucket, with a ceiling-clamped adaptive full-size threshold gated on a minimum signal count."""
from dataclasses import dataclass

from bot.strategy.quote_quality import quote_freshness_pass, MIN_EXPECTED_MOVE_CUSHION


# ── Post-v2 refinement §1: credit-quality classification lanes ───────────────────────────────────
# Credit-as-%-of-wing thresholds (do NOT loosen -- partner "do not loosen yet" list).
ABSOLUTE_CREDIT_FLOOR = 0.08
STANDARD_PROBE_FLOOR = 0.10
FULL_SIZE_CREDIT_FLOOR = 0.115
LOW_CREDIT_MAX_QTY = 1
PROBE_SIZE_MULTIPLIER = 0.40


def classify_credit_quality(
    credit_ratio: float,
    adaptive_full_size_threshold: float,
) -> tuple[str, float, int | None]:
    """Post-v2 refinement §1. Classify a candidate by credit-as-%-of-wing into one of four lanes.

    Returns (tier, size_multiplier, maximum_quantity):
      - credit_ratio < ABSOLUTE_CREDIT_FLOOR (0.08)          -> ("reject", 0.0, 0)
      - credit_ratio < STANDARD_PROBE_FLOOR (0.10)           -> ("low_credit_safety", 0.25, 1)
        (a thin 0.08-0.10 band tradeable at quarter size, cap 1, gated by low_credit_safety_pass)
      - credit_ratio < adaptive_full_size_threshold          -> ("probe", 0.40, None)
      - otherwise                                            -> ("full", 1.0, None)
    """
    if credit_ratio < ABSOLUTE_CREDIT_FLOOR:
        return "reject", 0.0, 0
    if credit_ratio < STANDARD_PROBE_FLOOR:
        return "low_credit_safety", 0.25, LOW_CREDIT_MAX_QTY
    if credit_ratio < adaptive_full_size_threshold:
        return "probe", PROBE_SIZE_MULTIPLIER, None
    return "full", 1.0, None


def low_credit_safety_pass(
    *,
    target_to_cost_ratio: float,
    package_width_ratio: float,
    quote_age_seconds: float,
    cushion_atr: float,
    expected_move_cushion: float,
    short_delta: float,
    defensive_market_state: bool,
) -> bool:
    """Post-v2 refinement §1. Additional hard gate a low_credit_safety candidate (0.08-0.10 band)
    must clear before it may trade at quarter size. Keyword-only so every call site is explicit and
    a missing/renamed input can't silently slot into the wrong position.

    Advisor Step 1: quote_age_seconds and expected_move_cushion are now genuinely plumbed (see
    bot/strategy/quote_quality.py) and BOTH accept None meaning "could not be determined". None
    always fails its condition -- an unknown quote age is not fresh, and an uncomputable expected
    move is not a cushion, leaving the short-delta branch as the only remaining way through the OR.
    short_delta None likewise fails (no delta, no fallback)."""
    return (
        target_to_cost_ratio >= 4.5
        and package_width_ratio <= 0.20
        and quote_freshness_pass(quote_age_seconds)
        and cushion_atr >= 1.15
        and ((expected_move_cushion is not None
              and expected_move_cushion >= MIN_EXPECTED_MOVE_CUSHION)
             or (short_delta is not None and short_delta <= 0.30))
        and not defensive_market_state
    )


def dte_bucket(dte: int) -> str:
    if dte <= 5:
        return "4-5"
    if dte <= 8:
        return "6-8"
    if dte <= 11:
        return "9-11"
    return "12+"


def _percentile(xs, p):
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return None
    k = (n - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, n - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def full_size_threshold(prior_ratios, floor, ceiling, min_signals, p=40):
    """§3: warmup -- with fewer than `min_signals` prior candidates, use the fixed `floor` (not
    enough data to adapt yet). Once warmed up, adapt to the pth percentile of prior candidate
    ratios, clamped to [floor, ceiling] so a hot or cold run of signals can't drag the threshold
    out of the sane range."""
    if not prior_ratios or len(prior_ratios) < min_signals:
        return floor
    return min(ceiling, max(floor, _percentile(prior_ratios, p)))


def credit_tier(credit, wing_width, min_ratio, full_threshold, probe_mult):
    """Return a size multiplier: 0.0 (reject), probe_mult, or 1.0."""
    ratio = credit / wing_width
    if ratio < min_ratio:
        return 0.0
    if ratio < full_threshold:
        return probe_mult
    return 1.0


# ── Post-v2 refinement §12: candidate-signal dedup for REPORTING ─────────────────────────────────
# A bot polling every 60s re-evaluates the SAME spread dozens of times a day. Counting each poll as
# a distinct opportunity turns every opportunity statistic into a polling-frequency indicator, so
# §12 keeps raw polling evaluations and UNIQUE candidate opportunities as two separate counters
# ("Do not use raw polling evaluations in profitability reports").
CREDIT_RATIO_REEVALUATION_CHANGE = 0.005   # a ratio move this large re-counts an otherwise-same key


def candidate_key(trading_date: str, expiry: str, short_strike: float, long_strike: float,
                  timestamp) -> tuple:
    """Post-v2 refinement §12. Identity of a candidate for dedup purposes: same day, expiry, strike
    pair and 15-minute bucket -> the same opportunity, however many times it was polled.

    The bucket is (hour, minute // 15) -- HOUR-QUALIFIED, so 10:01 and 11:01 do not collide into one
    "sub-bucket 0"; the four windows per hour stay distinct across the session."""
    minute_bucket = (timestamp.hour, timestamp.minute // 15)
    return (trading_date, expiry, round(short_strike, 3), round(long_strike, 3), minute_bucket)


def is_new_candidate(*, key: tuple, ratio: float, seen: dict) -> bool:
    """True when `key` names a materially distinct opportunity from what has been seen: the key is
    unseen (new strike pair / expiry / 15-min bucket / day), or its credit ratio has moved by at
    least CREDIT_RATIO_REEVALUATION_CHANGE since the last sighting of that key.

    PURE -- does not mutate `seen`. Use record_candidate to count AND advance the anchor."""
    if key not in seen:
        return True
    # Round the DELTA before comparing: binary floats make an exact-threshold move land on either
    # side depending on direction (0.125-0.12 == 0.005000000000000004 but 0.12-0.115 ==
    # 0.004999999999999991), so a raw >= would count an upward 0.005 move and drop the identical
    # downward one. Rounding to 6 places is well inside the 4-place ratios we compare.
    return round(abs(ratio - seen[key]), 6) >= CREDIT_RATIO_REEVALUATION_CHANGE


def record_candidate(*, key: tuple, ratio: float, seen: dict, trading_date: str = None) -> bool:
    """Count a sighting: returns whether it is a NEW unique opportunity (see is_new_candidate) and
    records `ratio` as the key's anchor either way.

    The anchor ADVANCES on every sighting, new or not. That matters: against a frozen first-seen
    anchor a candidate drifting 0.004 per poll would never trip the threshold, while one that
    wandered away and back would re-count. Advancing measures drift since the LAST look.

    `trading_date` (when given) prunes keys from prior trading days -- keys embed the date, so
    without pruning `seen` would grow without bound across a long-running process."""
    if trading_date is not None:
        for k in [k for k in seen if k[0] != trading_date]:
            del seen[k]
    new = is_new_candidate(key=key, ratio=ratio, seen=seen)
    seen[key] = ratio
    return new


@dataclass(frozen=True)
class CreditObservation:
    """Post-v2 refinement §13, adopted per the advisor's Decision 1 (Option B): the structured audit
    record of an adaptive-credit-history observation.

    Option B deliberately stops short of full spec fidelity. This record is EMITTED TO THE LOG only
    -- `credit_ratio_history` keeps storing bare floats and `BotState` never carries the record --
    because the rolling threshold consumes ratios alone, so adopting the record as the persisted
    structure would mean migrating live state on a real-money bot to buy auditability and nothing
    else. Frozen: an audit record that a later stage can mutate is not an audit record."""
    timestamp: object            # datetime; kept untyped to avoid importing datetime into this pure module
    expiry: str
    dte_bucket: str
    short_strike: float
    long_strike: float
    expected_executable_credit: float
    credit_ratio: float
    candidate_key: tuple

    def log_fields(self) -> dict:
        """Flat, CSV-safe projection for the decision/research log. The candidate key is joined into
        a single string because a tuple would render as a Python repr in CSV and be painful to query;
        the bucket renders as "<hour>-<quarter>"."""
        d, e, short, long_, bucket = self.candidate_key
        return {
            "event": "CREDIT_OBS",
            "date": d,
            "expiry": self.expiry,
            "short": self.short_strike,
            "long": self.long_strike,
            "credit_ratio": self.credit_ratio,
            "obs_dte_bucket": self.dte_bucket,
            "obs_expected_executable_credit": self.expected_executable_credit,
            "obs_candidate_key": f"{d}|{e}|{short}|{long_}|{bucket[0]}-{bucket[1]}",
        }


def bucket15_of(timestamp) -> int:
    """The 15-minute research window as a single JSON-safe int, identifying exactly the same window
    as candidate_key's (hour, minute // 15) tuple (post-v2 refinement §13 alignment).

    An int rather than the tuple because this value lives in the JSON-persisted `credit_obs_last` --
    a tuple would reload as a list and never compare equal again (the same trap handled explicitly
    for seen_candidate_keys in state_store)."""
    return timestamp.hour * 4 + timestamp.minute // 15


def should_record_observation(last, *, date, expiry, short, long, ratio, bucket15):
    """Record a new adaptive-credit-history observation only when this candidate is materially
    different from the last recorded one for its DTE bucket: new day/expiry/strike, a new 15-minute
    research window, or a credit-ratio move >= 0.5 percentage point. Dedups repeated near-identical
    observations of the same spread across a polling day (partner review v2 item 3).

    Post-v2 refinement §13 states this as "new_candidate_key or |ratio move| >= 0.005" -- the first
    four components below ARE §12's candidate_key minus the bucket, and `bucket15` (via bucket15_of)
    is now that key's window, so the two dedup rules agree by construction. The ratio comparison
    delegates to is_new_candidate so there is ONE threshold implementation, including its rounding
    (a raw float >= was asymmetric: it recorded a +0.005 move but not an identical -0.005 one)."""
    if last is None:
        return True
    if (date, expiry, short, long) != (last["date"], last["expiry"], last["short"], last["long"]):
        return True
    if bucket15 != last["bucket15"]:
        return True
    return is_new_candidate(key=(), ratio=ratio, seen={(): last["ratio"]})
