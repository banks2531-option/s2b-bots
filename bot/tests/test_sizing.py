import pytest
from bot.sizing import contracts_for_risk


def test_contracts_floor_division():
    # $20k equity, 10% risk = $2000 budget; $700 max-loss/contract -> 2 contracts
    assert contracts_for_risk(20_000, 700, 0.10) == 2


def test_contracts_minimum_one():
    # budget smaller than one contract's max loss still returns 1
    assert contracts_for_risk(5_000, 900, 0.10) == 1


def test_contracts_rejects_nonpositive_maxloss():
    with pytest.raises(ValueError):
        contracts_for_risk(20_000, 0, 0.10)


from bot.sizing import regime_adjusted_risk_pct


def test_regime_normal_keeps_base():
    # calm vol: percentile 0.5, no spike -> base unchanged
    assert regime_adjusted_risk_pct(0.10, vix_pct_rank=0.5, vix_1d_change=0.02) == 0.10


def test_regime_high_percentile_halves():
    # VIX above 80th percentile -> halve
    assert regime_adjusted_risk_pct(0.10, vix_pct_rank=0.85, vix_1d_change=0.0) == 0.05


def test_regime_spike_halves():
    # 1-day VIX change > +15% -> halve even if percentile low
    assert regime_adjusted_risk_pct(0.10, vix_pct_rank=0.40, vix_1d_change=0.20) == 0.05


def test_regime_none_inputs_keep_base():
    assert regime_adjusted_risk_pct(0.10, vix_pct_rank=None, vix_1d_change=None) == 0.10
