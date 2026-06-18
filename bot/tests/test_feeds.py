from bot.app.feeds import pick_weekly_expiry


def test_pick_weekly_expiry_next_friday_min_dte():
    # Monday 2026-06-15: the Friday 2026-06-19 is 4 DTE -> ok
    assert pick_weekly_expiry("2026-06-15", min_dte=4) == "2026-06-19"


def test_pick_weekly_expiry_skips_too_near_friday():
    # Thursday 2026-06-18: that-week Friday (06-19) is only 1 DTE -> roll to next Friday 06-26
    assert pick_weekly_expiry("2026-06-18", min_dte=4) == "2026-06-26"
