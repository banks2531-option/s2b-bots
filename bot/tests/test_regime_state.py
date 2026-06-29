# bot/tests/test_regime_state.py
from dataclasses import asdict
from bot.regime.state import RegimeState


def test_regime_state_defaults_and_serializable():
    s = RegimeState()
    # raw context defaults are "unknown/neutral", decision fields are no-op (full size, no pause)
    assert s.trend_bias == "neutral"
    assert s.flow_bias == "neutral"
    assert s.flow_extreme is False
    assert s.size_multiplier == 1.0
    assert s.pause_entries is False
    assert s.degross_target == 0.0
    assert s.kill is False
    d = asdict(s)                      # must be flat-serializable for CSV logging
    assert "stress_score" in d and "vix_term_slope" in d
