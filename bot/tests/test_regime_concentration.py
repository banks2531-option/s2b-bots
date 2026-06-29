# bot/tests/test_regime_concentration.py
from bot.regime.concentration import concentration_score, exposure_map
from bot.strategy.manage import ManagedPosition


def _p(short, expiry):
    return ManagedPosition("SPY", short, short - 10, credit=1.4, qty=7, expiry=expiry)


def test_concentration_high_when_book_is_identical():
    # 5 near-identical spreads (same expiry, tight strike band) -> high concentration
    book = [_p(727, "2026-07-10"), _p(728, "2026-07-10"), _p(727, "2026-07-10"),
            _p(728, "2026-07-10"), _p(728, "2026-07-10")]
    assert concentration_score(book) > 0.8


def test_concentration_low_when_spread_out():
    book = [_p(700, "2026-07-10"), _p(720, "2026-07-17"), _p(740, "2026-07-24")]
    assert concentration_score(book) < 0.5
    assert concentration_score([]) == 0.0


def test_exposure_map_groups_by_expiry_and_band():
    book = [_p(727, "2026-07-10"), _p(728, "2026-07-10")]
    m = exposure_map(book, band=3.0)
    assert m[("2026-07-10", 726.0)] == 2          # both fall in the same $3 band (floor to 3)
