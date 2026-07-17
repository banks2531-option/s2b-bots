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
