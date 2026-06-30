# bot/tests/test_regime_trend.py
from bot.regime.trend import trend_bias, atr_pct


def _bars(closes):
    return [{"high": c + 1, "low": c - 1, "close": c} for c in closes]


def test_trend_bias_up_down_neutral():
    up = _bars(list(range(100, 160)))            # steadily rising, price well above MAs
    assert trend_bias(up) == "up"
    down = _bars(list(range(160, 100, -1)))      # steadily falling
    assert trend_bias(down) == "down"
    flat = _bars([130] * 60)
    assert trend_bias(flat) == "neutral"
    assert trend_bias([]) == "neutral"           # no data -> unknown/neutral


def test_trend_regime_above_below_unknown():
    from bot.regime.trend import trend_regime
    up = _bars(list(range(100, 350)))            # 250 rising bars -> latest well above 200d MA
    assert trend_regime(up) == "risk_on"
    down = _bars(list(range(350, 100, -1)))      # 250 falling bars -> latest below 200d MA
    assert trend_regime(down) == "risk_off"
    assert trend_regime(_bars([100] * 150)) == "unknown"   # < 200 bars


def test_atr_pct_positive_fraction():
    bars = _bars([100 + i for i in range(30)])
    p = atr_pct(bars)
    assert p is not None and 0 < p < 0.5
    assert atr_pct([]) is None
