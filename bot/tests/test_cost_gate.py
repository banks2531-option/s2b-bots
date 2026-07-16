"""TDD for Task 2.3: transaction-cost profitability gate (partner review v2 §5). Gross 50%-of-credit
target must clear MIN_TARGET_TO_COST_RATIO x the estimated round-trip cost (commissions + slippage),
or the candidate is rejected. Behind deps.features.transaction_cost_gate."""
import dataclasses

from bot.strategy.cost_gate import cost_gate_eval, estimated_round_trip_cost
from bot.features import S2bFeatures


# defaults: commission_per_contract_per_leg_per_side=0.65, expected_entry_slippage=0.03,
# take_profit_percent=0.50, min_target_to_cost_ratio=4.0 ->
# round_trip_cost = 0.65*2*2 + (0.03+0.03)*100 = 2.60 + 6.00 = 8.60


def test_estimated_round_trip_cost_defaults():
    f = S2bFeatures()
    assert estimated_round_trip_cost(f) == 8.60


def test_cost_gate_rejects_thin_credit():
    f = S2bFeatures()
    # expected_credit=0.30 -> gross_target=15.0; rt=8.60; ratio=1.744 < 4.0 -> rejected
    ev = cost_gate_eval(0.30, f)
    assert ev["gross_target"] == 15.0
    assert ev["round_trip_cost"] == 8.60
    assert ev["target_to_cost_ratio"] == 1.744
    assert ev["passes"] is False


def test_cost_gate_passes_rich_credit():
    f = S2bFeatures()
    # expected_credit=2.00 -> gross_target=100.0; rt=8.60; ratio=11.628 >= 4.0 -> passes
    ev = cost_gate_eval(2.00, f)
    assert ev["gross_target"] == 100.0
    assert ev["target_to_cost_ratio"] == 11.628
    assert ev["passes"] is True


def test_cost_gate_boundary_exactly_at_ratio_passes():
    f = S2bFeatures()
    # gross_target == exactly 4.0 * rt (34.4) -> expected_credit = 0.688
    ev = cost_gate_eval(0.688, f)
    assert ev["target_to_cost_ratio"] == 4.0
    assert ev["passes"] is True


def test_cost_gate_ratio_configurable_3x():
    f = S2bFeatures(min_target_to_cost_ratio=3.0)
    # expected_credit=0.60 -> gross_target=30.0; rt=8.60; ratio=3.488 >= 3.0 -> passes
    ev = cost_gate_eval(0.60, f)
    assert ev["target_to_cost_ratio"] == 3.488
    assert ev["passes"] is True


def test_cost_gate_ratio_configurable_5x_rejects_same_credit_that_passed_at_4x():
    f = S2bFeatures(min_target_to_cost_ratio=5.0)
    # expected_credit=0.688 passed at ratio=4.0 (default); with min_ratio=5.0 the same 4.0 ratio fails
    ev = cost_gate_eval(0.688, f)
    assert ev["target_to_cost_ratio"] == 4.0
    assert ev["passes"] is False


def test_cost_gate_zero_credit_rejected():
    f = S2bFeatures()
    ev = cost_gate_eval(0.0, f)
    assert ev["gross_target"] == 0.0
    assert ev["target_to_cost_ratio"] == 0.0
    assert ev["passes"] is False


# ── full_size_threshold defensive guard (trivial fix from the last review) ──────────────────────

def test_full_size_threshold_empty_history_and_zero_min_signals_returns_floor_no_raise():
    from bot.strategy.credit_quality import full_size_threshold
    assert full_size_threshold([], 0.115, 0.14, min_signals=0) == 0.115


# ── round-trip commission consistency (P0 fix item 1) ───────────────────────────────────────────

def test_round_trip_commission_is_two_legs_two_sides():
    from bot.strategy.cost_gate import (one_side_commission_per_contract,
                                        round_trip_commission_per_contract)
    from bot.features import S2bFeatures
    f = S2bFeatures()  # 0.65 default
    # document the model as relationships, not a restated literal:
    #   one side  = 2 legs  ->  per-leg-per-side x 2
    #   round trip = 2 sides -> one side x 2
    assert one_side_commission_per_contract(f) == f.commission_per_contract_per_leg_per_side * 2
    assert round_trip_commission_per_contract(f) == one_side_commission_per_contract(f) * 2


def test_round_trip_cost_uses_full_commission():
    from bot.strategy.cost_gate import estimated_round_trip_cost
    from bot.features import S2bFeatures
    f = S2bFeatures()
    assert estimated_round_trip_cost(f) == 8.60
