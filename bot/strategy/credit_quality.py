"""Credit-quality tiering (partner review §2): size by credit-as-%-of-wing, adaptive per DTE bucket."""


def dte_bucket(dte: int) -> str:
    if dte <= 5:
        return "4-5"
    if dte <= 8:
        return "6-8"
    return "9-11"


def _percentile(xs, p):
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return None
    k = (n - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, n - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def full_size_threshold(prior_ratios, floor=0.115, p=40):
    hp = _percentile(prior_ratios, p)
    return max(floor, hp) if hp is not None else floor


def credit_tier(credit, wing_width, min_ratio, full_threshold, probe_mult):
    """Return a size multiplier: 0.0 (reject), probe_mult, or 1.0."""
    ratio = credit / wing_width
    if ratio < min_ratio:
        return 0.0
    if ratio < full_threshold:
        return probe_mult
    return 1.0
