from bot.strategy.s2b import is_entry_day


def test_monday_is_entry_day():
    assert is_entry_day("2026-06-15") is True   # Monday


def test_other_days_not_entry():
    assert is_entry_day("2026-06-16") is False   # Tuesday
    assert is_entry_day("2026-06-19") is False   # Friday
