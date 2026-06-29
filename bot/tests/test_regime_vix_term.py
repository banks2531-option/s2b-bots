# bot/tests/test_regime_vix_term.py
from bot.regime.vix_term import term_slope, pct_rank


def test_term_slope_backwardation_is_negative():
    # front VIX above 3M = stress/backwardation -> negative slope
    assert term_slope(22.0, 20.0) < 0
    # contango (front below 3M) -> positive
    assert term_slope(18.0, 20.0) > 0
    assert term_slope(20.0, 0.0) == 0.0          # guard divide-by-zero


def test_pct_rank_basic():
    series = [10, 12, 14, 16, 18, 20]
    assert pct_rank(20, series) == 1.0           # at the top
    assert pct_rank(10, series) <= 0.34          # near the bottom
    assert pct_rank(99, []) is None              # empty -> unknown
