"""Aggregate dollar-risk budget sizing + book limits (partner review v2 §9)."""
import pytest

from bot.portfolio.risk_budget import (
    structural_max_loss_per_contract,
    planned_stop_loss_per_contract,
    remaining_stop_risk,
    size_qty,
    cap_to_budgets,
)
from bot.strategy.manage import ManagedPosition
from bot.features import S2bFeatures


# ── planned_stop_loss_per_contract: min(structural, 2x+slippage) ───────────────

def test_planned_stop_loss_picks_2x_slippage_when_smaller():
    # credit=2.0, wing=10: structural=(10-2)*100=800; (2*2+0.10)*100=410 -> min is 410
    assert planned_stop_loss_per_contract(2.0, 10.0, 0.10) == pytest.approx(410.0)


def test_planned_stop_loss_picks_structural_when_smaller():
    # credit=9.5, wing=10: structural=(10-9.5)*100=50; (2*9.5+0.10)*100=1910 -> min is 50
    assert planned_stop_loss_per_contract(9.5, 10.0, 0.10) == 50.0


def test_structural_max_loss_per_contract():
    assert structural_max_loss_per_contract(10.0, 2.0) == 800.0


# ── remaining_stop_risk: never negative, excludes already-incurred loss ────────

def test_remaining_stop_risk_normal_case():
    # entry_credit=2.0 -> stop_debit=6.0; current_debit=3.0, slippage=0.10, qty=2
    # (6.0 - 3.0 + 0.10) * 100 * 2 = 620.0
    assert remaining_stop_risk(2.0, 3.0, 2, 0.10) == 620.0


def test_remaining_stop_risk_never_negative_excludes_incurred_loss():
    # already deep underwater beyond the 3x-credit stop debit: (6.0 - 7.0 + 0.10) < 0 -> clamp to 0.
    # The incurred loss beyond the stop point must NOT count as future stop risk.
    assert remaining_stop_risk(2.0, 7.0, 1, 0.10) == 0.0


def test_remaining_stop_risk_zero_at_exact_stop_debit():
    # at exactly stop_debit (no slippage cushion consumed), remaining is just the slippage buffer
    assert remaining_stop_risk(2.0, 6.0, 1, 0.10) == 10.0


# ── size_qty: min of entry-stop and structural constraints, times multiplier ───

def test_size_qty_entry_constraint_binds():
    f = S2bFeatures()   # max_entry_stop_risk_pct=0.0075, expected_stop_slippage=0.10
    # credit=2.0, wing=10, risk_equity=100_000: psl=410, struct=800
    # qty_entry = floor(100000*0.0075/410) = floor(1.829) = 1
    # qty_structural = floor(100000*0.05/800) = floor(6.25) = 6 -> entry constraint binds
    assert size_qty(100_000.0, 2.0, 10.0, f) == 1


def test_size_qty_structural_constraint_binds():
    f = S2bFeatures()
    # credit=0.5, wing=10: struct=950, psl=min(950, (2*0.5+0.10)*100=110)=110
    # qty_entry = floor(200000*0.0075/110) = floor(13.636) = 13
    # qty_structural = floor(200000*0.05/950) = floor(10.526) = 10 -> structural constraint binds
    assert size_qty(200_000.0, 0.5, 10.0, f) == 10


def test_size_qty_applies_quality_multiplier():
    f = S2bFeatures()
    base = size_qty(100_000.0, 2.0, 10.0, f, quality_multiplier=1.0)
    assert base == 1
    scaled = size_qty(100_000.0, 0.5, 10.0, f, quality_multiplier=0.5)
    # base (structural-bound) qty at credit=0.5, risk_equity=100_000: struct=950, psl=110
    # qty_entry=floor(100000*0.0075/110)=6, qty_structural=floor(100000*0.05/950)=5 -> base=5
    assert scaled == 2   # floor(5 * 0.5) = 2


def test_size_qty_probe_keeps_one_candidate_not_zero():
    # SPEC §2 CORRECTION: this test previously asserted the OLD buggy behavior -- that base qty 1
    # times a 0.4 probe multiplier floored to 0 at the size_qty level, silently turning the probe
    # TIER into a rejection TIER. Per spec §2 the probe must yield a >=1 CANDIDATE instead. That
    # candidate is NOT forced past risk caps: it still flows into cap_to_budgets downstream, which
    # can cap it to 0. size_qty only produces the candidate.
    f = S2bFeatures()
    # base qty is 1 (see test_size_qty_entry_constraint_binds); the 0.4 probe now yields 1, not 0
    assert size_qty(100_000.0, 2.0, 10.0, f, quality_multiplier=0.4) == 1


def test_size_qty_zero_or_negative_constraints_reject():
    f = S2bFeatures()
    assert size_qty(100_000.0, 10.0, 10.0, f) == 0   # credit == wing_width -> structural is 0


def test_size_qty_uses_features_max_trade_structural_risk_pct_not_hardcoded():
    # same inputs as test_size_qty_structural_constraint_binds (struct=950, qty_entry=13), but with
    # max_trade_structural_risk_pct doubled to 0.10 -> qty_structural = floor(200000*0.10/950) = 21,
    # so the entry constraint (13) now binds instead of the structural one (proof the field, not a
    # hardcoded 0.05, drives this branch).
    f = S2bFeatures(max_trade_structural_risk_pct=0.10)
    assert size_qty(200_000.0, 0.5, 10.0, f) == 13


def test_size_qty_default_max_trade_structural_risk_pct_is_unchanged():
    assert S2bFeatures().max_trade_structural_risk_pct == 0.05


# ── QuantityCap + quantity_cap_from_budget (spec §3/§4): every gate becomes a "how many fit" cap ──

def test_quantity_cap_from_budget_basic_remaining_and_qty():
    from bot.portfolio.risk_budget import quantity_cap_from_budget, QuantityCap
    cap = quantity_cap_from_budget("same_day_stop", current_exposure=1000.0, limit=3000.0,
                                   incremental_risk_per_contract=250.0)
    assert isinstance(cap, QuantityCap)
    assert cap.name == "same_day_stop"
    assert cap.remaining_capacity == 2000.0        # 3000 - 1000
    assert cap.maximum_qty == 8                     # floor(2000 / 250)
    assert cap.current_exposure == 1000.0
    assert cap.limit == 3000.0
    assert cap.incremental_risk_per_contract == 250.0


def test_quantity_cap_from_budget_zero_incremental_gives_zero_qty():
    # spec §4: incremental_risk_per_contract <= 0 -> qty = 0 (never divide-by-zero, never "unlimited")
    from bot.portfolio.risk_budget import quantity_cap_from_budget
    cap = quantity_cap_from_budget("total_structural", current_exposure=0.0, limit=5000.0,
                                   incremental_risk_per_contract=0.0)
    assert cap.maximum_qty == 0
    assert cap.remaining_capacity == 5000.0


def test_quantity_cap_from_budget_limit_at_or_below_exposure_gives_zero():
    from bot.portfolio.risk_budget import quantity_cap_from_budget
    cap = quantity_cap_from_budget("expiry_stop", current_exposure=3000.0, limit=3000.0,
                                   incremental_risk_per_contract=250.0)
    assert cap.remaining_capacity == 0.0
    assert cap.maximum_qty == 0
    over = quantity_cap_from_budget("expiry_stop", current_exposure=3200.0, limit=3000.0,
                                    incremental_risk_per_contract=250.0)
    assert over.remaining_capacity == 0.0           # clamped, never negative
    assert over.maximum_qty == 0


# ── budget_quantity_caps: the four budgets, each as a QuantityCap (spec §3/§4) ────────────────────

def test_budget_quantity_caps_returns_four_named_caps_empty_book():
    from bot.portfolio.risk_budget import budget_quantity_caps
    f = S2bFeatures()
    caps = budget_quantity_caps(credit=2.0, wing_width=10.0, expiry="2026-07-18",
                                today="2026-07-14", open_positions=[], mark_fn=lambda p: p.credit,
                                risk_equity=100_000.0, f=f)
    names = [c.name for c in caps]
    assert names == ["same_day_stop", "expiry_stop", "total_stop", "total_structural"]
    # empty book -> each cap = floor(limit / per_contract). credit=2.0, wing=10:
    #   planned_stop_loss=410, structural=800
    #   same_day  = floor(100000*0.02/410)  = 4
    #   expiry    = floor(100000*0.03/410)  = 7
    #   total     = floor(100000*0.04/410)  = 9
    #   structural= floor(100000*0.15/800)  = 18
    by = {c.name: c for c in caps}
    assert by["same_day_stop"].maximum_qty == 4
    assert by["expiry_stop"].maximum_qty == 7
    assert by["total_stop"].maximum_qty == 9
    assert by["total_structural"].maximum_qty == 18
    assert by["total_structural"].incremental_risk_per_contract == 800.0
    assert by["same_day_stop"].incremental_risk_per_contract == pytest.approx(410.0)


def test_budget_quantity_caps_min_agrees_with_cap_to_budgets():
    # Cross-check: the min of the four QuantityCaps (capped at requested qty) equals the quantity
    # cap_to_budgets would permit for the same inputs -- the two must never diverge.
    from bot.portfolio.risk_budget import budget_quantity_caps
    f = S2bFeatures()
    risk_equity = 100_000.0
    today = "2026-07-14"
    expiry = "2026-07-18"
    open_positions = [_pos(2.0, 5, expiry, "2026-07-10")]
    mark_fn = lambda p: p.credit
    caps = budget_quantity_caps(credit=2.0, wing_width=10.0, expiry=expiry, today=today,
                                open_positions=open_positions, mark_fn=mark_fn,
                                risk_equity=risk_equity, f=f)
    requested = 5
    min_cap = min(c.maximum_qty for c in caps)
    permitted = min(requested, min_cap)
    r = cap_to_budgets(requested, 2.0, 10.0, expiry, today, open_positions, mark_fn, risk_equity, f)
    assert permitted == int(r) == 2                 # matches the nearly-full-expiry-budget case


def test_budget_quantity_caps_foreign_exposure_counts_only_total_budgets():
    # A large foreign structural exposure must shrink the total_structural cap but NOT the
    # same-day / expiry caps (which stay this-bot-only). Mirrors cap_to_budgets' foreign handling.
    from bot.portfolio.risk_budget import budget_quantity_caps
    f = S2bFeatures()
    caps = budget_quantity_caps(credit=2.0, wing_width=10.0, expiry="2026-07-18",
                                today="2026-07-14", open_positions=[], mark_fn=lambda p: p.credit,
                                risk_equity=100_000.0, f=f,
                                foreign_exposure={"stop": 0.0, "structural": 14200.0})
    by = {c.name: c for c in caps}
    # structural budget 15000, foreign 14200 -> remaining 800 -> floor(800/800) = 1
    assert by["total_structural"].maximum_qty == 1
    # same-day/expiry untouched by foreign structural
    assert by["same_day_stop"].maximum_qty == 4
    assert by["expiry_stop"].maximum_qty == 7


# ── apply_quality_multiplier: probe never rounds a valid base qty to 0 (spec §2) ────────────────

def test_probe_size_never_rounds_valid_base_qty_to_zero():
    from bot.portfolio.risk_budget import apply_quality_multiplier
    assert apply_quality_multiplier(base_qty=2, multiplier=0.40) == 1


def test_apply_quality_multiplier_full_size_and_maximum_and_zero_base():
    from bot.portfolio.risk_budget import apply_quality_multiplier
    assert apply_quality_multiplier(4, 1.0) == 4                    # full tier unchanged
    assert apply_quality_multiplier(2, 0.40, maximum_qty=1) == 1    # maximum_qty caps candidate
    assert apply_quality_multiplier(0, 0.40) == 0                   # zero base -> zero


# ── size_to_risk_limits: assemble caps, reduce (not reject), name the binding gate (spec §10) ────

def test_probe_minimum_does_not_override_zero_risk_capacity():
    from bot.portfolio.risk_budget import size_to_risk_limits, QuantityCap, apply_quality_multiplier
    requested = apply_quality_multiplier(2, 0.40)   # == 1
    cap = QuantityCap("gap_1_5atr", 0, 4300, 4320, 20, 250)
    result = size_to_risk_limits(requested_qty=requested, caps=[cap])
    assert result.final_qty == 0 and not result.allowed


def test_oversized_order_is_reduced_not_rejected():
    from bot.portfolio.risk_budget import size_to_risk_limits, QuantityCap
    caps = [QuantityCap("expiry_stop", 1, 1800, 2160, 360, 250),
            QuantityCap("total_stop", 4, 1000, 2880, 1880, 250)]
    result = size_to_risk_limits(requested_qty=3, caps=caps)
    assert result.allowed and result.final_qty == 1 and result.limiting_gate == "expiry_stop"


def test_size_to_risk_limits_requested_qty_zero_is_blocked():
    from bot.portfolio.risk_budget import size_to_risk_limits, QuantityCap
    cap = QuantityCap("total_stop", 4, 1000, 2880, 1880, 250)
    result = size_to_risk_limits(requested_qty=0, caps=[cap])
    assert not result.allowed
    assert result.final_qty == 0
    assert result.limiting_gate == "requested_qty"
    assert result.limiting_quantity == 0
    assert result.reason == "quality-adjusted quantity is zero"


def test_size_to_risk_limits_empty_caps_is_unconstrained():
    from bot.portfolio.risk_budget import size_to_risk_limits
    result = size_to_risk_limits(requested_qty=3, caps=[])
    assert result.allowed
    assert result.final_qty == 3
    assert result.limiting_gate is None
    assert result.limiting_quantity is None
    assert result.reason == "ok"


def test_size_to_risk_limits_reason_when_binding_cap_permits_zero():
    from bot.portfolio.risk_budget import size_to_risk_limits, QuantityCap
    cap = QuantityCap("gap_2atr", 0, 5000, 5000, 0, 300)
    result = size_to_risk_limits(requested_qty=2, caps=[cap])
    assert not result.allowed
    assert result.final_qty == 0
    assert result.limiting_gate == "gap_2atr"
    assert result.reason == "gap_2atr permits zero contracts"


# ── DecisionOutcome vocabulary + classify_outcome (spec §11) ─────────────────────────────────────

def test_decision_outcome_constants_exact_values():
    from bot.portfolio.risk_budget import DecisionOutcome
    assert DecisionOutcome.ALLOWED_FULL == "allowed_full"
    assert DecisionOutcome.ALLOWED_REDUCED == "allowed_reduced"
    assert DecisionOutcome.BLOCKED_ZERO_CAPACITY == "blocked_zero_capacity"
    assert DecisionOutcome.BLOCKED_CREDIT_QUALITY == "blocked_credit_quality"
    assert DecisionOutcome.BLOCKED_TRANSACTION_COST == "blocked_transaction_cost"
    assert DecisionOutcome.BLOCKED_QUOTE_QUALITY == "blocked_quote_quality"
    assert DecisionOutcome.BLOCKED_DUPLICATE == "blocked_duplicate"
    assert DecisionOutcome.BLOCKED_REGIME == "blocked_regime"


def test_classify_outcome_full_reduced_zero():
    from bot.portfolio.risk_budget import classify_outcome, DecisionOutcome
    assert classify_outcome(requested_qty=3, final_qty=3) == DecisionOutcome.ALLOWED_FULL
    assert classify_outcome(requested_qty=3, final_qty=1) == DecisionOutcome.ALLOWED_REDUCED
    assert classify_outcome(requested_qty=3, final_qty=0) == DecisionOutcome.BLOCKED_ZERO_CAPACITY


# ── cap_to_budgets: reduce qty to fit book limits, 0 when a budget is exhausted ─

def _pos(credit, qty, expiry, entry_date):
    return ManagedPosition("SPY", 568.0, 558.0, credit, qty, expiry, entry_date=entry_date)


def test_cap_to_budgets_reduces_qty_for_nearly_full_expiry_budget():
    f = S2bFeatures()   # max_expiry_stop_risk_pct=0.03, max_same_day_stop_risk_pct=0.02
    risk_equity = 100_000.0
    today = "2026-07-14"
    expiry = "2026-07-18"
    # existing book position: same expiry as the proposed trade, opened on a PRIOR day (so it does
    # NOT eat the same-day budget), credit=2.0, qty=5, breakeven mark (no incurred loss yet).
    # stop risk = (3*2.0 - 2.0 + 0.10) * 100 * 5 = 4.10*100*5 = 2050 -> expiry_stop=2050
    open_positions = [_pos(2.0, 5, expiry, "2026-07-10")]
    mark_fn = lambda p: p.credit   # breakeven
    # proposed trade: credit=2.0, wing=10 -> per-contract stop = planned_stop_loss_per_contract = 410
    # expiry budget = 100000*0.03 = 3000; remaining = 3000-2050 = 950 -> floor(950/410) = 2
    q = cap_to_budgets(5, 2.0, 10.0, expiry, today, open_positions, mark_fn, risk_equity, f)
    assert q == 2


def test_cap_to_budgets_returns_zero_when_budget_exhausted():
    f = S2bFeatures()
    risk_equity = 100_000.0
    today = "2026-07-14"
    expiry = "2026-07-18"
    # book already at/over the expiry stop budget: stop risk = (6.0-2.0+0.10)*100*8 = 3280 > 3000 budget
    open_positions = [_pos(2.0, 8, expiry, "2026-07-10")]
    mark_fn = lambda p: p.credit
    q = cap_to_budgets(5, 2.0, 10.0, expiry, today, open_positions, mark_fn, risk_equity, f)
    assert q == 0


def test_cap_to_budgets_empty_book_keeps_full_qty():
    f = S2bFeatures()
    risk_equity = 100_000.0
    q = cap_to_budgets(1, 2.0, 10.0, "2026-07-18", "2026-07-14", [], lambda p: p.credit,
                        risk_equity, f)
    assert q == 1


def test_cap_to_budgets_qty_zero_input_stays_zero():
    f = S2bFeatures()
    assert cap_to_budgets(0, 2.0, 10.0, "2026-07-18", "2026-07-14", [], lambda p: p.credit,
                           100_000.0, f) == 0


def test_cap_to_budgets_no_foreign_exposure_arg_matches_default_none_regression():
    """Omitting foreign_exposure entirely must behave exactly like passing foreign_exposure=None
    (which in turn must behave exactly like before this param existed) -- a pure regression check."""
    f = S2bFeatures()
    risk_equity = 100_000.0
    expiry = "2026-07-18"
    open_positions = [_pos(2.0, 5, expiry, "2026-07-10")]
    mark_fn = lambda p: p.credit
    q_omitted = cap_to_budgets(5, 2.0, 10.0, expiry, "2026-07-14", open_positions, mark_fn, risk_equity, f)
    q_explicit_none = cap_to_budgets(5, 2.0, 10.0, expiry, "2026-07-14", open_positions, mark_fn,
                                      risk_equity, f, foreign_exposure=None)
    q_explicit_zero = cap_to_budgets(5, 2.0, 10.0, expiry, "2026-07-14", open_positions, mark_fn,
                                      risk_equity, f, foreign_exposure={"stop": 0.0, "structural": 0.0})
    assert q_omitted == q_explicit_none == q_explicit_zero == 2   # same as
    # test_cap_to_budgets_reduces_qty_for_nearly_full_expiry_budget


def test_cap_to_budgets_large_foreign_exposure_tightens_total_stop_budget():
    f = S2bFeatures()   # max_total_stop_risk_pct=0.04 -> budget = 100000*0.04 = 4000
    risk_equity = 100_000.0
    expiry = "2026-07-18"
    # no book at all -- only foreign exposure eats the TOTAL stop budget.
    # foreign stop = 3600 -> remaining = 400 -> per-contract stop (credit=2.0, wing=10) = 410
    # -> floor(400/410) = 0 -> capped to 0 by the total-stop budget alone.
    foreign = {"stop": 3600.0, "structural": 0.0}
    q = cap_to_budgets(5, 2.0, 10.0, expiry, "2026-07-14", [], lambda p: p.credit,
                        risk_equity, f, foreign_exposure=foreign)
    assert q == 0


def test_cap_to_budgets_large_foreign_exposure_tightens_total_structural_budget():
    f = S2bFeatures()   # max_total_structural_risk_pct=0.15 -> budget = 100000*0.15 = 15000
    risk_equity = 100_000.0
    expiry = "2026-07-18"
    # foreign structural = 14200 -> remaining = 800 -> per-contract structural (credit=2.0, wing=10)
    # = 800 -> floor(800/800) = 1 -> capped to 1 by the total-structural budget alone.
    foreign = {"stop": 0.0, "structural": 14200.0}
    q = cap_to_budgets(5, 2.0, 10.0, expiry, "2026-07-14", [], lambda p: p.credit,
                        risk_equity, f, foreign_exposure=foreign)
    assert q == 1


def test_cap_to_budgets_foreign_exposure_combines_with_own_book_in_total_budgets():
    f = S2bFeatures()
    risk_equity = 100_000.0
    expiry = "2026-07-18"
    # own book: same expiry, PRIOR day, credit=2.0 qty=3 -> stop=(6.0-2.0+0.10)*100*3=1230;
    # structural = (10-2.0)*100*3 = 2400. Plus foreign stop=2500 -> total_stop=3730;
    # total budget = 100000*0.04=4000 -> remaining=270 -> floor(270/410)=0
    open_positions = [_pos(2.0, 3, expiry, "2026-07-10")]
    foreign = {"stop": 2500.0, "structural": 0.0}
    q = cap_to_budgets(5, 2.0, 10.0, expiry, "2026-07-14", open_positions, lambda p: p.credit,
                        risk_equity, f, foreign_exposure=foreign)
    assert q == 0


# ── cap_to_budgets returns a RiskBudgetResult naming the limiting budget (item 7) ───────────────

# Book already heavy in the SAME expiry as the proposed trade (entered on a PRIOR day, so it does
# NOT eat the same-day budget): credit=2.25, qty=4, mark=1.2 (fixed by the test's mark_fn) ->
# remaining_stop_risk = (3*2.25 - 1.2 + 0.10) * 100 * 4 = 5.65*100*4 = 2260. With risk_equity=72000
# and default S2bFeatures: expiry budget = 72000*0.03 = 2160 (already exceeded by 2260 -> expiry
# caps the proposed trade to 0), same-day budget = 72000*0.02 = 1440 (unconstrained, same_day_stop=0
# since the book position isn't same-day), total-stop budget = 72000*0.04 = 2880 (remaining 620 /
# 250 per-contract = floor 2, still allows the full qty=2), total-structural budget = 72000*0.15 =
# 10800 (nowhere close to binding). So expiry_stop is the sole/first budget that binds.
OVER_EXPIRY_BOOK = [_pos(2.25, 4, "2026-07-24", "2026-07-10")]


def test_cap_to_budgets_returns_limiting_reason():
    from bot.portfolio.risk_budget import cap_to_budgets, RiskBudgetResult
    from bot.features import S2bFeatures
    r = cap_to_budgets(qty=2, credit=1.20, wing_width=10, expiry="2026-07-24", today="2026-07-15",
                       open_positions=OVER_EXPIRY_BOOK, mark_fn=lambda p: 1.2,
                       risk_equity=72000, f=S2bFeatures())
    assert isinstance(r, RiskBudgetResult)
    assert r.allowed_quantity == 0
    assert r.limiting_budget == "expiry_stop"
    assert int(r) == 0                # int-compat: existing callers keep working
    assert r == 0                     # equality with int works


def test_cap_to_budgets_returns_none_limiting_budget_when_unconstrained():
    from bot.portfolio.risk_budget import cap_to_budgets, RiskBudgetResult
    f = S2bFeatures()
    r = cap_to_budgets(3, 2.0, 10.0, "2026-07-18", "2026-07-14", [], lambda p: p.credit,
                       100_000.0, f)
    assert isinstance(r, RiskBudgetResult)
    assert r.limiting_budget is None
    assert r.allowed_quantity == 3
    assert int(r) == 3
    assert r == 3


def test_cap_to_budgets_int_compat_matches_old_int_returning_logic():
    """Back-compat: int(cap_to_budgets(...)) must equal whatever the old bare-int-returning
    implementation would have produced for a simple constrained case (same numbers as
    test_cap_to_budgets_reduces_qty_for_nearly_full_expiry_budget, which asserted q == 2)."""
    from bot.portfolio.risk_budget import cap_to_budgets
    f = S2bFeatures()
    risk_equity = 100_000.0
    today = "2026-07-14"
    expiry = "2026-07-18"
    open_positions = [_pos(2.0, 5, expiry, "2026-07-10")]
    mark_fn = lambda p: p.credit
    r = cap_to_budgets(5, 2.0, 10.0, expiry, today, open_positions, mark_fn, risk_equity, f)
    assert int(r) == 2


def test_cap_to_budgets_excludes_incurred_loss_from_book_risk():
    """A position already blown through its stop (mark >= 3x credit) contributes ZERO remaining
    stop risk to the book -- it must not eat the new trade's budget. (The requested qty=5 is still
    capped to 4 here by the SAME-DAY budget, which the proposed trade itself always counts against
    regardless of the book -- 100000*0.02=2000 same-day budget / 410 per-contract = floor(4.87) = 4.)"""
    f = S2bFeatures()
    risk_equity = 100_000.0
    expiry = "2026-07-18"
    open_positions = [_pos(2.0, 5, expiry, "2026-07-10")]
    mark_fn = lambda p: 999.0   # far beyond any stop -> remaining_stop_risk is clamped to 0
    q = cap_to_budgets(5, 2.0, 10.0, expiry, "2026-07-14", open_positions, mark_fn, risk_equity, f)
    assert q == 4   # expiry/total budgets are untouched by the blown-out position; only the
                     # proposed trade's own same-day exposure limits it


# ── wired into run_entry_cycle behind deps.features.aggregate_risk_budget ──────

from datetime import datetime

from bot.app.orchestrator import BotState, Deps, run_entry_cycle
from bot.strategy.s2b import OptionQuote
from bot.risk_gate import AccountState


def _custom_chain(short_bid, long_ask):
    """Minimal 568/558 SPY chain (spot=575, atr=6.0) with a controlled credit = short_bid - long_ask."""
    return [
        OptionQuote(568.0, 0.36, short_bid, short_bid + 0.10),
        OptionQuote(558.0, 0.18, long_ask - 0.10, long_ask),
    ]


def _acct(today, conc):
    return AccountState(100_000.0, 100_000.0, 0.0, conc, 0.0, {}, today)


def _deps(**over):
    base = dict(
        get_spot=lambda sym: 575.0, get_atr=lambda sym: 6.0,
        get_chain=lambda sym, exp: _custom_chain(4.00, 2.00),  # credit=2.00
        pick_expiry=lambda today: "2026-06-19",
        get_vix_regime=lambda: (0.5, 0.01), account_state=_acct,
        mark_position=lambda p: 3.0, dte_of=lambda p, today: 5,
        open_spread=lambda payload: "filled", close_spread=lambda p, a: "filled",
        broker_positions=lambda: [], broker_equity=lambda: 100_000.0,
        bot_equity=lambda: 100_000.0, alert_sink=lambda alerts: None,
        risk_equity=lambda: 100_000.0,
    )
    base.update(over)
    return Deps(**base)


MONDAY = datetime(2026, 6, 15, 10, 5)   # Monday 10:05, expiry 2026-06-19


def test_orchestrator_aggregate_risk_budget_on_sizes_via_budget():
    state = BotState()
    f = S2bFeatures(aggregate_risk_budget=True)
    d = _deps(features=f)
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    assert len(state.open_positions) == 1
    # budgeted qty (see test_size_qty_entry_constraint_binds: credit=2.0, risk_equity=100_000 -> 1)
    # vs. the old contracts_for_risk path, which would give floor(100000*0.10/800)=12 -- proof the
    # budget path, not the legacy path, is driving sizing.
    assert state.open_positions[0].qty == 1


def test_orchestrator_aggregate_risk_budget_on_rejects_when_capped_to_zero():
    state = BotState()
    f = S2bFeatures(aggregate_risk_budget=True)
    # a tiny risk_equity floors size_qty's entry-stop-risk qty to 0 (100000 -> 1 normally; 1000 -> 0)
    d = _deps(features=f, risk_equity=lambda: 1_000.0)
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "risk_budget"
    assert state.open_positions == []


def test_orchestrator_foreign_spy_position_tightens_total_budget_for_new_entry():
    """partner review v2 §2: a foreign SPY spread at the broker (not opened by this bot) must count
    toward this bot's TOTAL stop/structural budget and can, by itself, reject an entry that would
    otherwise have filled (see test_orchestrator_aggregate_risk_budget_on_sizes_via_budget: with NO
    foreign exposure this exact setup fills 1 contract)."""
    state = BotState()
    f = S2bFeatures(aggregate_risk_budget=True)
    # unrelated strikes/expiry (this bot has no open positions yet, so nothing to accidentally
    # match-and-exclude anyway); credit=0.0 (foreign/unknown) -> conservative full-width stop =
    # 10*100*4 = 4000, which alone consumes the entire max_total_stop_risk_pct budget
    # (100_000 * 0.04 = 4000), leaving 0 room for the proposed trade's 410-per-contract stop.
    foreign_spread = ManagedPosition("SPY", 600.0, 590.0, 0.0, 4, "2099-01-01")
    d = _deps(features=f, account_spy_spreads=lambda: [foreign_spread])

    state, info = run_entry_cycle(state, d, MONDAY)

    assert info == "risk_budget"
    assert state.open_positions == []


def test_orchestrator_no_account_spy_spreads_feed_is_unaffected_regression():
    """Deps.account_spy_spreads defaults to None (no feed wired) -> foreign exposure must be treated
    as zero, exactly reproducing the pre-Fix-A behavior."""
    state = BotState()
    f = S2bFeatures(aggregate_risk_budget=True)
    d = _deps(features=f)   # no account_spy_spreads override -> None
    assert d.account_spy_spreads is None

    state, info = run_entry_cycle(state, d, MONDAY)

    assert info == "filled"
    assert state.open_positions[0].qty == 1   # unchanged from test_orchestrator_aggregate_risk_budget_on_sizes_via_budget


def test_orchestrator_aggregate_risk_budget_off_by_default_unchanged():
    state = BotState()
    d = _deps()   # features defaults to S2bFeatures() -> aggregate_risk_budget False
    assert d.features.aggregate_risk_budget is False
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    assert len(state.open_positions) == 1
    # legacy contracts_for_risk path: max_loss=(10-2.00)*100=800, risk=0.10 (normal VIX)
    # -> floor(100000*0.10/800) = 12
    assert state.open_positions[0].qty == 12
