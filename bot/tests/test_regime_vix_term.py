# bot/tests/test_regime_vix_term.py
from bot.regime import vix_term as vt
from bot.regime.vix_term import term_slope, pct_rank, fetch_vix_term


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


def test_fetch_vix_term_caches_within_ttl(monkeypatch):
    # the expensive yfinance fetch must run at most once per TTL window, not every tick
    calls = {"n": 0}
    def fake_download(lookback):
        calls["n"] += 1
        return (20.0, 0.5, 0.05)
    monkeypatch.setattr(vt, "_download_term", fake_download)
    vt._TERM_CACHE.update({"ts": 0.0, "val": (None, None, None)})   # reset cache
    a = fetch_vix_term()
    b = fetch_vix_term()
    assert a == (20.0, 0.5, 0.05) and b == a
    assert calls["n"] == 1                          # second call served from cache


def test_fetch_vix_term_returns_last_good_on_failure(monkeypatch):
    # a later fetch that hangs/errors must fall back to the last good value, not blank the signal
    def good(lookback):
        return (18.0, 0.4, 0.03)
    monkeypatch.setattr(vt, "_download_term", good)
    vt._TERM_CACHE.update({"ts": 0.0, "val": (None, None, None)})
    assert fetch_vix_term() == (18.0, 0.4, 0.03)
    def boom(lookback):
        raise ConnectionError("yfinance down")
    monkeypatch.setattr(vt, "_download_term", boom)
    vt._TERM_CACHE["ts"] = 0.0                       # force past the TTL so it refetches
    assert fetch_vix_term() == (18.0, 0.4, 0.03)     # last good, not (None,None,None)
