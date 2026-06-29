# bot/tests/test_regime_equity_dd.py
from bot.regime.equity_dd import drawdown_from_peak


def test_drawdown_from_peak():
    assert drawdown_from_peak(110, peak=110) == 0.0          # at a new high
    assert round(drawdown_from_peak(99, peak=110), 4) == 0.1  # 10% below peak
    assert drawdown_from_peak(120, peak=110) == 0.0          # new high resets to 0
    assert drawdown_from_peak(100, peak=0) == 0.0            # no peak yet -> 0
