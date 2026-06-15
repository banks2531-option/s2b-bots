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


# I1 — negative/zero equity must raise in sizing
def test_contracts_rejects_nonpositive_equity():
    with pytest.raises(ValueError):
        contracts_for_risk(-100, 700, 0.10)


# I2 — both VIX conditions firing still caps at a single halve
def test_regime_both_conditions_single_halve():
    # vix_pct_rank=0.9 (>0.80) AND vix_1d_change=0.2 (>0.15) -> returns base/2, not base/4
    result = regime_adjusted_risk_pct(0.10, vix_pct_rank=0.9, vix_1d_change=0.2)
    assert result == 0.05


# m3 — VIX rank exactly at threshold is NOT halved (strict >)
def test_regime_rank_at_threshold_unchanged():
    # vix_pct_rank=0.80 exactly -> 0.80 is NOT > 0.80, so base unchanged
    result = regime_adjusted_risk_pct(0.10, vix_pct_rank=0.80, vix_1d_change=0.0)
    assert result == 0.10
