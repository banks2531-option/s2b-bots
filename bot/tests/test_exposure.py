"""TDD for Task 1.2: allocated equity + account-level SPY exposure supervisor (partner review v2 §2)."""
from dataclasses import dataclass

from bot.portfolio.exposure import risk_equity, account_spy_exposure, foreign_spy_exposure


def test_risk_equity_caps_at_allocation_when_broker_equity_is_higher():
    assert risk_equity(72000, 80000) == 72000


def test_risk_equity_uses_broker_equity_when_lower_than_allocation():
    assert risk_equity(72000, 60000) == 60000


@dataclass
class _FakeSpread:
    short_strike: float
    long_strike: float
    credit: float
    qty: int
    expiry: str = "2026-07-18"


def test_account_spy_exposure_empty_list_is_zero():
    result = account_spy_exposure([])
    assert result == {"structural": 0.0, "stop": 0.0}


def test_account_spy_exposure_sums_structural_and_stop_across_own_and_foreign_positions():
    # Own position: $10 wing, $1.70 credit, qty 2 -> structural = (10-1.70)*100*2 = 1660
    # stop = 2 * credit * 100 * qty = 2*1.70*100*2 = 680
    own = _FakeSpread(short_strike=568.0, long_strike=558.0, credit=1.70, qty=2)
    # Foreign position (reconstructed from broker, credit unknown -> 0.0): $10 wing, qty 1
    # structural = (10-0)*100*1 = 1000 ; stop = conservative full width = 10*100*1 = 1000
    foreign = _FakeSpread(short_strike=570.0, long_strike=560.0, credit=0.0, qty=1)

    result = account_spy_exposure([own, foreign])

    assert result["structural"] == 1660.0 + 1000.0
    assert result["stop"] == 680.0 + 1000.0


def test_account_spy_exposure_foreign_only_uses_conservative_full_width_stop():
    foreign = _FakeSpread(short_strike=575.0, long_strike=565.0, credit=0.0, qty=3)
    result = account_spy_exposure([foreign])
    # structural = (10-0)*100*3 = 3000 ; stop = 10*100*3 = 3000 (conservative, not zero)
    assert result["structural"] == 3000.0
    assert result["stop"] == 3000.0


# ── foreign_spy_exposure: broker-wide spreads MINUS this bot's own book (partner review v2 §2) ──

def test_foreign_spy_exposure_excludes_own_positions_matched_by_strikes_and_expiry():
    own = _FakeSpread(short_strike=568.0, long_strike=558.0, credit=1.70, qty=2, expiry="2026-07-18")
    # broker-wide list includes this bot's own spread (same strikes+expiry+qty, credit unknown as
    # reconstructed from the broker) plus one truly-foreign spread at different strikes.
    broker_own_echo = _FakeSpread(short_strike=568.0, long_strike=558.0, credit=0.0, qty=2, expiry="2026-07-18")
    foreign = _FakeSpread(short_strike=570.0, long_strike=560.0, credit=0.0, qty=1, expiry="2026-07-18")

    result = foreign_spy_exposure([broker_own_echo, foreign], own_positions=[own])

    # only the foreign spread counts: structural=(10-0)*100*1=1000; stop=full-width=10*100*1=1000
    assert result == {"structural": 1000.0, "stop": 1000.0}


def test_foreign_spy_exposure_different_expiry_is_not_treated_as_own():
    own = _FakeSpread(short_strike=568.0, long_strike=558.0, credit=1.70, qty=2, expiry="2026-07-18")
    # same strikes, but a DIFFERENT expiry -> not a match -> counts as foreign
    other_expiry = _FakeSpread(short_strike=568.0, long_strike=558.0, credit=0.0, qty=1, expiry="2026-07-25")

    result = foreign_spy_exposure([other_expiry], own_positions=[own])

    assert result == {"structural": 1000.0, "stop": 1000.0}


def test_foreign_spy_exposure_no_foreign_positions_is_zero():
    own = _FakeSpread(short_strike=568.0, long_strike=558.0, credit=1.70, qty=2, expiry="2026-07-18")
    broker_own_echo = _FakeSpread(short_strike=568.0, long_strike=558.0, credit=0.0, qty=2, expiry="2026-07-18")

    result = foreign_spy_exposure([broker_own_echo], own_positions=[own])

    assert result == {"structural": 0.0, "stop": 0.0}


def test_foreign_spy_exposure_empty_broker_list_is_zero():
    assert foreign_spy_exposure([], own_positions=[]) == {"structural": 0.0, "stop": 0.0}


# ── quantity-aware foreign matching (Priority-0 fix item 6): a broker spread matching an own
# position's (short, long, expiry) key must still count the QUANTITY DIFFERENCE as foreign, not be
# excluded wholesale ──

def _pos(short_strike, long_strike, expiry, qty, credit=0.0):
    return _FakeSpread(short_strike=short_strike, long_strike=long_strike, credit=credit,
                        qty=qty, expiry=expiry)


def test_foreign_exposure_counts_quantity_difference():
    from bot.portfolio.exposure import foreign_spy_exposure
    own = _pos(743, 733, "2026-07-24", qty=8, credit=1.39)
    broker = _pos(743, 733, "2026-07-24", qty=10, credit=0.0)  # 2 extra foreign contracts
    e = foreign_spy_exposure([broker], [own])
    assert e["stop"] == 10.0 * 100 * 2       # 2000.0 (credit 0 -> width*100*fq)
    assert e["structural"] == 10.0 * 100 * 2 # 2000.0


def test_foreign_exposure_zero_when_broker_qty_le_own():
    from bot.portfolio.exposure import foreign_spy_exposure
    own = _pos(743, 733, "2026-07-24", qty=8, credit=1.39)
    broker = _pos(743, 733, "2026-07-24", qty=8, credit=0.0)
    e = foreign_spy_exposure([broker], [own])
    assert e["stop"] == 0.0 and e["structural"] == 0.0


def test_foreign_exposure_sums_multiple_own_lots_at_same_key_before_diff():
    # Two own lots at the SAME (short,long,expiry) key -> own_qty accumulates to 8; broker holds 10
    # -> only 2 foreign, not (10-5)=5. Verifies own-side accumulation before the qty diff.
    from bot.portfolio.exposure import foreign_spy_exposure
    own_a = _pos(743, 733, "2026-07-24", qty=5, credit=1.39)
    own_b = _pos(743, 733, "2026-07-24", qty=3, credit=1.41)
    broker = _pos(743, 733, "2026-07-24", qty=10, credit=0.0)
    e = foreign_spy_exposure([broker], [own_a, own_b])
    assert e["stop"] == 10.0 * 100 * 2       # 2000.0
    assert e["structural"] == 10.0 * 100 * 2 # 2000.0


def test_foreign_exposure_sums_duplicate_broker_rows_at_same_key_before_diff():
    # Two broker rows at the SAME key -> broker_qty accumulates to 10 BEFORE subtracting own_qty(8),
    # so foreign = 2. Without pre-aggregation each row would subtract own_qty and undercount to 0.
    from bot.portfolio.exposure import foreign_spy_exposure
    own = _pos(743, 733, "2026-07-24", qty=8, credit=1.39)
    broker_a = _pos(743, 733, "2026-07-24", qty=6, credit=0.0)
    broker_b = _pos(743, 733, "2026-07-24", qty=4, credit=0.0)
    e = foreign_spy_exposure([broker_a, broker_b], [own])
    assert e["stop"] == 10.0 * 100 * 2       # 2000.0
    assert e["structural"] == 10.0 * 100 * 2 # 2000.0
