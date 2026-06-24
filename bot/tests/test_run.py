from bot.app.run_s2b import fetch_spot, fetch_atr


def test_fetch_spot_returns_last():
    http = lambda m, p, params=None, data=None: {"quotes": {"quote": {"symbol": "SPY", "last": 575.25}}}
    assert fetch_spot(http, "SPY") == 575.25


def test_fetch_atr_from_history():
    hist = {"history": {"day": [
        {"date": "2026-06-10", "open": 1, "high": 12, "low": 10, "close": 11},
        {"date": "2026-06-11", "open": 1, "high": 13, "low": 11, "close": 12},
        {"date": "2026-06-12", "open": 1, "high": 14, "low": 12, "close": 13},
    ]}}
    http = lambda m, p, params=None, data=None: hist
    # every bar's true range = 2.0 -> ATR(2) = 2.0
    assert fetch_atr(http, "SPY", "2026-06-15", n=2) == 2.0
