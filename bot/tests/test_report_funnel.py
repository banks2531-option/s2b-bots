"""T10 (spec §17, advisor botbnextsteps Step 3): the reconciled daily funnel.

The report must account for every candidate from "the bot woke up" through to "contracts actually
filled", so a session with no trades can be explained rather than guessed at.

The reconciliation rule that gives the section its value: contracts requested must equal contracts
filled plus every contract removed along the way, attributed to the stage that removed it. If those
do not add up, a stage is lying.
"""
import pytest

from bot.ops.report import build_funnel


def _d(**kw):
    base = {"event": "DECISION", "date": "2026-07-20"}
    base.update(kw)
    return base


# ── activity counters ───────────────────────────────────────────────────────────────────────────

def test_activity_counters_come_from_the_latest_values_not_a_sum():
    """The counters are cumulative on BotState, so the report takes the highest seen in the window --
    summing per-row snapshots would multiply them by the number of decisions."""
    rows = [_d(entry_cycles_started=10, raw_candidate_evaluations=4,
               unique_candidate_opportunities=2, decision="filled"),
            _d(entry_cycles_started=30, raw_candidate_evaluations=9,
               unique_candidate_opportunities=3, decision="risk_budget")]
    a = build_funnel(rows)["activity"]
    assert a["entry_cycles_started"] == 30
    assert a["raw_candidate_evaluations"] == 9
    assert a["unique_candidate_opportunities"] == 3


def test_unique_strike_pairs_and_expirations_are_counted():
    rows = [_d(short="568.0", long="558.0", expiry="2026-07-24", decision="filled"),
            _d(short="568.0", long="558.0", expiry="2026-07-24", decision="filled"),
            _d(short="565.0", long="555.0", expiry="2026-07-31", decision="risk_budget")]
    a = build_funnel(rows)["activity"]
    assert a["unique_strike_pairs"] == 2
    assert a["unique_expirations_evaluated"] == 2


# ── credit / quality gates ──────────────────────────────────────────────────────────────────────

def test_credit_gate_outcomes_are_counted_by_tier_and_reason():
    rows = [
        _d(decision="credit_too_low", credit_ratio="0.05"),                          # below 8%
        _d(decision="credit_too_low", credit_ratio="0.09", lc_safety_pass="False"),   # lane failed
        _d(decision="filled", credit_ratio="0.09", lc_safety_pass="True"),            # lane qualified
        _d(decision="filled", credit_quality_mult="0.4"),                             # probe
        _d(decision="filled", credit_quality_mult="1.0"),                             # full
        _d(decision="cost_gate"),
        _d(decision="quote_wide"),
        _d(decision="quote_invalid"),
        _d(decision="trend_paused"),
    ]
    g = build_funnel(rows)["credit_gates"]
    assert g["rejected_below_absolute_floor"] == 1
    assert g["rejected_low_credit_safety"] == 1
    assert g["qualified_low_credit_safety"] == 1
    assert g["qualified_probe"] == 1
    assert g["qualified_full_size"] == 1
    assert g["rejected_transaction_cost"] == 1
    assert g["rejected_quote_quality"] == 2
    assert g["rejected_trend"] == 1


# ── quantity outcomes and limiting caps ─────────────────────────────────────────────────────────

def test_quantity_outcomes_split_full_reduced_probe_and_blocked():
    rows = [_d(decision="filled", decision_outcome="allowed_full", requested_qty="3", final_qty="3"),
            _d(decision="filled", decision_outcome="allowed_reduced", requested_qty="5", final_qty="2"),
            _d(decision="filled", decision_outcome="allowed_reduced", requested_qty="4", final_qty="1"),
            _d(decision="risk_budget", decision_outcome="blocked_zero_capacity",
               requested_qty="3", final_qty="0")]
    q = build_funnel(rows)["quantity_outcomes"]
    assert q["allowed_at_requested_quantity"] == 1
    assert q["allowed_at_reduced_quantity"] == 2
    assert q["allowed_as_one_contract_probe"] == 1     # the reduced-to-1 case
    assert q["blocked_at_zero_capacity"] == 1


def test_limiting_caps_are_counted_for_zero_capacity_blocks_only():
    """A cap that merely REDUCED a trade is not the same as one that BLOCKED it; conflating them
    would make gap stress look like it is killing trades it actually only trimmed."""
    rows = [_d(decision="risk_budget", decision_outcome="blocked_zero_capacity",
               limiting_gate="gap_1_5atr", final_qty="0"),
            _d(decision="risk_budget", decision_outcome="blocked_zero_capacity",
               limiting_gate="expiry_stop", final_qty="0"),
            _d(decision="filled", decision_outcome="allowed_reduced",
               limiting_gate="gap_2atr", final_qty="2")]
    caps = build_funnel(rows)["limiting_caps"]
    assert caps["gap_1_5atr"] == 1
    assert caps["expiry_stop"] == 1
    assert "gap_2atr" not in caps


# ── contract reconciliation ─────────────────────────────────────────────────────────────────────

def test_contract_reconciliation_balances():
    """requested == filled + removed at each stage. This is the check that catches a lying stage."""
    rows = [
        _d(decision="filled", requested_qty="5", quality_adjusted_qty="4", final_qty="2",
           limiting_gate="expiry_stop"),
        _d(decision="filled", requested_qty="3", quality_adjusted_qty="3", final_qty="1",
           limiting_gate="gap_1_5atr"),
        {"event": "OPEN", "date": "2026-07-20", "qty": "2", "status": "filled"},
        {"event": "OPEN", "date": "2026-07-20", "qty": "1", "status": "filled"},
    ]
    c = build_funnel(rows)["contracts"]
    assert c["requested"] == 8
    assert c["quality_adjusted"] == 7
    assert c["risk_approved"] == 3
    assert c["filled"] == 3
    assert c["removed_by_credit_sizing"] == 1          # 8 -> 7
    assert c["removed_by_aggregate_risk"] == 2         # the expiry_stop case: 4 -> 2
    assert c["removed_by_gap_stress"] == 2             # the gap case: 3 -> 1
    assert c["requested"] == (c["filled"] + c["removed_by_credit_sizing"]
                              + c["removed_by_aggregate_risk"] + c["removed_by_gap_stress"])
    assert c["reconciles"] is True


def test_reconciliation_flags_an_imbalance_rather_than_hiding_it():
    rows = [_d(decision="filled", requested_qty="5", quality_adjusted_qty="5", final_qty="5"),
            {"event": "OPEN", "date": "2026-07-20", "qty": "1", "status": "filled"}]
    c = build_funnel(rows)["contracts"]
    assert c["reconciles"] is False       # 5 approved, only 1 filled and nothing removed


# ── order funnel ────────────────────────────────────────────────────────────────────────────────

def test_order_funnel_distinguishes_allowed_from_filled():
    """Allowed is not filled. Reporting them as one number is how a bot looks like it is trading
    when it is really just qualifying candidates that never fill."""
    rows = [_d(decision="filled", final_qty="2"), _d(decision="filled", final_qty="1"),
            {"event": "OPEN", "date": "2026-07-20", "qty": "2", "status": "filled"},
            {"event": "OPEN", "date": "2026-07-20", "qty": "1", "status": "cancelled"}]
    o = build_funnel(rows)["orders"]
    assert o["candidates_allowed"] == 2
    assert o["orders_submitted"] == 2
    assert o["orders_filled"] == 1
    assert o["orders_cancelled"] == 1
    assert o["qualified_but_unfilled"] == 1


# ── gap model comparison ────────────────────────────────────────────────────────────────────────

def test_gap_comparison_summarises_the_black_scholes_difference():
    rows = [_d(decision="filled", bs_qty_1_5="1", intrinsic_qty_1_5="3",
               bs_incremental_1_5="600.0", intrinsic_incremental_1_5="180.0",
               bs_zeroed_the_candidate="False"),
            _d(decision="risk_budget", bs_qty_1_5="0", intrinsic_qty_1_5="2",
               bs_incremental_1_5="700.0", intrinsic_incremental_1_5="200.0",
               bs_zeroed_the_candidate="True")]
    g = build_funnel(rows)["gap_comparison"]
    assert g["contracts_intrinsic_would_permit"] == 5
    assert g["contracts_black_scholes_permits"] == 1
    assert g["candidates_zeroed_by_black_scholes"] == 1
    assert g["avg_incremental_difference"] == pytest.approx(460.0)
    assert g["median_incremental_difference"] == pytest.approx(460.0)
    assert g["max_incremental_difference"] == pytest.approx(500.0)


def test_gap_comparison_absent_when_not_logged():
    assert build_funnel([_d(decision="filled")])["gap_comparison"] is None


# ── shadow + expirations ────────────────────────────────────────────────────────────────────────

def test_five_wide_shadow_results_are_summarised():
    rows = [{"event": "FIVE_WIDE_SHADOW", "date": "2026-07-20", "five_wide_available": "True",
             "five_wide_final_qty": "2", "ten_wide_limiting_gate": "gap_1_5atr"},
            {"event": "FIVE_WIDE_SHADOW", "date": "2026-07-20", "five_wide_available": "True",
             "five_wide_final_qty": "0", "ten_wide_limiting_gate": "expiry_stop"}]
    s = build_funnel(rows)["five_wide_shadow"]
    assert s["evaluated"] == 2
    assert s["would_have_qualified"] == 1
    assert s["hypothetical_contracts"] == 2


def test_empty_log_produces_a_zeroed_funnel_not_a_crash():
    f = build_funnel([])
    assert f["activity"]["entry_cycles_started"] == 0
    assert f["contracts"]["requested"] == 0
