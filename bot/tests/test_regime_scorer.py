# bot/tests/test_regime_scorer.py
from bot.regime.state import RegimeState
from bot.regime.scorer import score


def test_score_calm_is_full_size_no_action():
    s = score(RegimeState(vix_pct_rank=0.3, vix_term_slope=0.05, trend_bias="up",
                          equity_drawdown=0.0, flow_extreme=False))
    assert s.size_multiplier == 1.0 and s.pause_entries is False
    assert s.degross_target == 0.0 and s.kill is False and s.risk_on is True


def test_score_stress_scales_and_pauses():
    s = score(RegimeState(vix_pct_rank=0.95, vix_term_slope=-0.08, trend_bias="down",
                          equity_drawdown=0.0, flow_extreme=True))
    assert s.size_multiplier < 1.0
    assert s.pause_entries is True            # risk-off composite
    assert s.stress_score > 0.5


def test_score_kill_and_degross_thresholds():
    # -6% drawdown -> de-gross + pause ; -10% -> kill + flat
    s6 = score(RegimeState(equity_drawdown=0.06))
    assert s6.degross_target >= 0.5 and s6.pause_entries is True and s6.kill is False
    s10 = score(RegimeState(equity_drawdown=0.10))
    assert s10.kill is True and s10.degross_target == 1.0
