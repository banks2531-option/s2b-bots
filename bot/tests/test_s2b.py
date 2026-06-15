from bot.strategy.s2b import is_entry_day


def test_monday_is_entry_day():
    assert is_entry_day("2026-06-15") is True   # Monday


def test_other_days_not_entry():
    assert is_entry_day("2026-06-16") is False   # Tuesday
    assert is_entry_day("2026-06-19") is False   # Friday
from bot.strategy.s2b import OptionQuote, S2bConfig, select_short_put


def _chain():
    # SPY puts; abs delta; strike, delta, bid, ask. spot assumed 575, ATR 6.
    return [
        OptionQuote(strike=572.0, delta=0.45, bid=4.50, ask=4.60),  # cushion 0.5 ATR -> excluded
        OptionQuote(strike=568.0, delta=0.36, bid=3.40, ask=3.50),  # cushion 1.17 ATR, delta near 0.35
        OptionQuote(strike=565.0, delta=0.30, bid=2.80, ask=2.90),  # cushion 1.67 ATR
        OptionQuote(strike=560.0, delta=0.22, bid=2.00, ask=2.10),  # cushion 2.5 ATR
    ]


def test_select_short_put_respects_cushion_and_delta():
    cfg = S2bConfig(target_delta=0.35, wing_width=10.0, min_cushion_atr=1.0)
    pick = select_short_put(_chain(), spot=575.0, atr=6.0, cfg=cfg)
    # 572 (delta 0.45, closest to nothing) is excluded by cushion; among cushion-OK,
    # 568 (delta 0.36) is closest to target 0.35
    assert pick.strike == 568.0


def test_select_short_put_none_when_no_cushion():
    cfg = S2bConfig(target_delta=0.35, wing_width=10.0, min_cushion_atr=5.0)  # demand 5 ATR
    assert select_short_put(_chain(), spot=575.0, atr=6.0, cfg=cfg) is None
