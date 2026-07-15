"""Credit-quality tiering (partner review v2 §3): size by credit-as-%-of-wing, adaptive per DTE
bucket, with a ceiling-clamped adaptive full-size threshold gated on a minimum signal count."""


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
