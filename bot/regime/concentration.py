"""Book concentration context: how clustered open positions are by expiry + strike band.
Used in Phase 1 to cap correlated exposure; in Phase 0 only logged."""
from collections import Counter


def exposure_map(positions, band: float = 3.0) -> dict:
    """{(expiry, banded_short_strike): count}. Strikes are floored to `band`-wide buckets so
    near-identical spreads (e.g. 727 & 728 in a $3 band) group together."""
    m = Counter()
    for p in positions:
        bucket = (float(p.short_strike) // band) * band
        m[(p.expiry, bucket)] += 1
    return dict(m)


def concentration_score(positions) -> float:
    """0..1: fraction of the book that piles into its single most-crowded expiry/band bucket.
    ~1/n when every position is unique, 1.0 when all positions pile into one bucket. 0.0 for an
    empty book."""
    n = len(positions)
    if n == 0:
        return 0.0
    m = exposure_map(positions)
    return round(max(m.values()) / n, 4)
