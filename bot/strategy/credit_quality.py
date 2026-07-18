"""Credit-quality tiering (partner review v2 §3): size by credit-as-%-of-wing, adaptive per DTE
bucket, with a ceiling-clamped adaptive full-size threshold gated on a minimum signal count."""


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
    a missing/renamed input can't silently slot into the wrong position."""
    return (
        target_to_cost_ratio >= 4.5
        and package_width_ratio <= 0.20
        and quote_age_seconds <= 2.0
        and cushion_atr >= 1.15
        and (expected_move_cushion >= 0.90 or short_delta <= 0.30)
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


def should_record_observation(last, *, date, expiry, short, long, ratio, bucket15):
    """Record a new adaptive-credit-history observation only when this candidate is materially
    different from the last recorded one for its DTE bucket: new day/expiry/strike, a new 15-minute
    research window, or a credit-ratio move >= 0.5 percentage point. Dedups repeated near-identical
    observations of the same spread across a polling day (partner review v2 item 3)."""
    if last is None:
        return True
    if (date, expiry, short, long) != (last["date"], last["expiry"], last["short"], last["long"]):
        return True
    if bucket15 != last["bucket15"]:
        return True
    return abs(ratio - last["ratio"]) >= 0.005   # 0.5 percentage point
