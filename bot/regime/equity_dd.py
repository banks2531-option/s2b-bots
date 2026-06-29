"""Own-equity drawdown context. drawdown_from_peak returns the fraction below the running peak
(>= 0). Phase 1 uses this for the de-gross/kill thresholds; Phase 0 only logs it."""


def drawdown_from_peak(equity: float, peak: float) -> float:
    """max(0, (peak - equity) / peak). 0.0 if peak <= 0 (no history yet) or at/above peak."""
    if not peak or peak <= 0:
        return 0.0
    return round(max(0.0, (peak - equity) / peak), 4)
