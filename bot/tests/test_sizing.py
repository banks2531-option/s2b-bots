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
