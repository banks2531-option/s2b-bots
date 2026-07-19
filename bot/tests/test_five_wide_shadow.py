"""T11 (spec §15, advisor botbnextsteps Step 4): $5-wide fallback in SHADOW.

Research only. When a valid $10-wide candidate clears every non-risk gate but is reduced to ZERO by
a risk cap, evaluate what a $5-wide spread at the same short strike would have done -- and log it.

It must never trade. Not "is configured not to trade" -- structurally cannot: the evaluator returns
a record, not an order, and carries an explicit non-submittable marker that the execution path
refuses.
"""
import pytest
from datetime import datetime

from bot.strategy.fallback import (should_evaluate_five_wide, evaluate_five_wide_shadow,
                                    ShadowOrderSubmissionError, assert_not_shadow, SHADOW_SOURCE)
from bot.features import S2bFeatures
from bot.strategy.s2b import OptionQuote
from bot.strategy.manage import ManagedPosition


TODAY = "2026-07-20"
SPOT, ATR = 575.0, 6.0
IV = lambda p: (0.20, 0.20)


class _Ten:
    """Stand-in for the evaluated $10-wide candidate."""
    def __init__(self, non_risk_pass=True, quality_qty=3, final_qty=0, gate="gap_1_5atr"):
        self.passed_non_risk_gates = non_risk_pass
        self.quality_adjusted_qty = quality_qty
        self.final_qty = final_qty
        self.limiting_gate = gate
        self.short_strike, self.long_strike = 568.0, 558.0
        self.expiry = "2026-07-24"


# ── trigger conditions ──────────────────────────────────────────────────────────────────────────

def test_triggers_only_when_risk_zeroed_an_otherwise_valid_candidate():
    assert should_evaluate_five_wide(_Ten()) is True


def test_does_not_trigger_when_the_candidate_actually_traded():
    assert should_evaluate_five_wide(_Ten(final_qty=2)) is False


def test_does_not_trigger_when_a_non_risk_gate_rejected_it():
    """Explicit in the directive: do NOT run the fallback for candidates rejected by poor credit,
    stale quotes or a defensive regime. A $5 wing does not fix a bad candidate -- it would just
    manufacture encouraging-looking research for trades that should never happen."""
    assert should_evaluate_five_wide(_Ten(non_risk_pass=False)) is False


def test_does_not_trigger_when_quality_sizing_already_produced_zero():
    assert should_evaluate_five_wide(_Ten(quality_qty=0)) is False


def test_does_not_trigger_without_a_named_limiting_gate():
    assert should_evaluate_five_wide(_Ten(gate=None)) is False


# ── evaluation ──────────────────────────────────────────────────────────────────────────────────

def _chain():
    """568 short with a 563 leg available for the $5 wing, plus 558 for the $10."""
    return [OptionQuote(570.0, 0.45, 6.00, 6.10, iv=0.12),
            OptionQuote(568.0, 0.36, 4.00, 4.10, iv=0.12),
            OptionQuote(563.0, 0.26, 2.60, 2.70, iv=0.12),
            OptionQuote(558.0, 0.18, 1.90, 2.00, iv=0.12)]


def _f(**over):
    base = dict(aggregate_risk_budget=True, credit_tiers=True, enable_five_wide_shadow=True)
    base.update(over)
    return S2bFeatures(**base)


def _eval(**over):
    kw = dict(ten_wide=_Ten(), chain=_chain(), spot=SPOT, atr=ATR, f=_f(),
              risk_equity=100_000.0, open_positions=[], foreign_positions=[],
              mark_fn=lambda p: 3.0, iv_fn=IV, today=TODAY, wing=5.0)
    kw.update(over)
    return evaluate_five_wide_shadow(**kw)


def test_builds_the_five_wide_at_the_same_short_strike():
    r = _eval()
    assert r["five_wide_short_strike"] == 568.0
    assert r["five_wide_long_strike"] == 563.0


def test_records_the_ten_wide_context_for_comparison():
    r = _eval()
    assert r["ten_wide_short_strike"] == 568.0
    assert r["ten_wide_final_qty"] == 0
    assert r["ten_wide_limiting_gate"] == "gap_1_5atr"


def test_records_the_full_five_wide_metric_set():
    r = _eval()
    for key in ("five_wide_credit", "five_wide_credit_ratio", "five_wide_target_to_cost_ratio",
                "five_wide_structural_risk", "five_wide_stop_risk",
                "five_wide_incremental_gap_risk", "five_wide_final_qty",
                "five_wide_limiting_gate"):
        assert key in r, key


def test_missing_long_leg_is_reported_not_fatal():
    """No 563 strike listed -> the $5 wing cannot be built. That is a finding, not a crash."""
    chain = [q for q in _chain() if q.strike != 563.0]
    r = _eval(chain=chain)
    assert r["five_wide_available"] is False
    assert r["five_wide_final_qty"] == 0


def test_available_flag_set_when_the_leg_exists():
    assert _eval()["five_wide_available"] is True


# ── it must not be able to trade ────────────────────────────────────────────────────────────────

def test_result_is_marked_as_shadow_and_non_submittable():
    r = _eval()
    assert r["source"] == SHADOW_SOURCE
    assert r["can_submit_order"] is False


def test_result_is_a_record_not_an_order():
    """Structural guarantee: there is no order object to submit even by accident."""
    r = _eval()
    assert isinstance(r, dict)
    assert "payload" not in r and "order" not in r


def test_execution_guard_refuses_a_shadow_candidate():
    with pytest.raises(ShadowOrderSubmissionError):
        assert_not_shadow({"source": SHADOW_SOURCE})


def test_execution_guard_passes_a_real_candidate():
    assert_not_shadow({"source": "primary"})
    assert_not_shadow({})


def test_shadow_disabled_returns_nothing():
    assert _eval(f=_f(enable_five_wide_shadow=False)) is None


# ── wired into run_entry_cycle: research fires, orders never do ─────────────────────────────────

from bot.app.orchestrator import BotState, Deps, run_entry_cycle
from bot.risk_gate import AccountState


def _oc_chain():
    return [OptionQuote(570.0, 0.45, 6.00, 6.10, iv=0.12),
            OptionQuote(568.0, 0.36, 4.00, 4.10, iv=0.12),
            OptionQuote(563.0, 0.26, 2.60, 2.70, iv=0.12),
            OptionQuote(558.0, 0.18, 1.90, 2.00, iv=0.12)]


def _oc_deps(features, submitted, **over):
    base = dict(
        get_spot=lambda sym: SPOT, get_atr=lambda sym: ATR,
        get_chain=lambda sym, exp: _oc_chain(),
        pick_expiry=lambda today: "2026-07-24",
        get_vix_regime=lambda: (0.5, 0.01),
        account_state=lambda today, conc: AccountState(100_000.0, 100_000.0, 0.0, conc, 0.0, {}, today),
        mark_position=lambda p: 3.0, dte_of=lambda p, today: 4,
        open_spread=lambda payload: submitted.append(payload) or "filled",
        close_spread=lambda p, a: "filled",
        broker_positions=lambda: [], broker_equity=lambda: 100_000.0,
        bot_equity=lambda: 100_000.0, alert_sink=lambda alerts: None,
        risk_equity=lambda: 100_000.0, max_open=9, max_entries_per_day=9, features=features,
    )
    base.update(over)
    return Deps(**base)


MON = datetime(2026, 7, 20, 10, 5)


def _blocking_book():
    """A prior-day position in the same expiry heavy enough to zero the $10 candidate's capacity."""
    return [ManagedPosition("SPY", 560.0, 550.0, 2.0, 9, "2026-07-24", entry_date="2026-07-17")]


def test_shadow_record_emitted_when_risk_zeroes_the_ten_wide():
    logged, submitted = [], []
    f = S2bFeatures(aggregate_risk_budget=True, decision_logging=True, enable_five_wide_shadow=True)
    d = _oc_deps(f, submitted, trade_log=lambda r: logged.append(r))
    state, info = run_entry_cycle(BotState(open_positions=_blocking_book()), d, MON)
    assert info == "risk_budget"
    shadows = [r for r in logged if r.get("event") == "FIVE_WIDE_SHADOW"]
    assert len(shadows) == 1
    assert shadows[0]["five_wide_short_strike"] == 568.0
    assert shadows[0]["can_submit_order"] is False


def test_no_order_is_ever_submitted_for_a_shadow_candidate():
    """The property that matters most: the cycle rejected, so NOTHING reached the broker -- the
    shadow evaluation added a log line and no order."""
    logged, submitted = [], []
    f = S2bFeatures(aggregate_risk_budget=True, decision_logging=True, enable_five_wide_shadow=True)
    d = _oc_deps(f, submitted, trade_log=lambda r: logged.append(r))
    state, info = run_entry_cycle(BotState(open_positions=_blocking_book()), d, MON)
    assert submitted == []
    assert not any((p.short_strike, p.long_strike) == (568.0, 563.0)
                   for p in state.open_positions)


def test_no_shadow_record_when_the_candidate_trades_normally():
    logged, submitted = [], []
    f = S2bFeatures(aggregate_risk_budget=True, decision_logging=True, enable_five_wide_shadow=True)
    state, info = run_entry_cycle(BotState(), _oc_deps(f, submitted,
                                                        trade_log=lambda r: logged.append(r)), MON)
    assert info == "filled"
    assert not [r for r in logged if r.get("event") == "FIVE_WIDE_SHADOW"]


def test_shadow_flag_off_emits_nothing():
    logged, submitted = [], []
    f = S2bFeatures(aggregate_risk_budget=True, decision_logging=True)
    run_entry_cycle(BotState(open_positions=_blocking_book()),
                    _oc_deps(f, submitted, trade_log=lambda r: logged.append(r)), MON)
    assert not [r for r in logged if r.get("event") == "FIVE_WIDE_SHADOW"]


def test_shadow_evaluation_does_not_change_the_rejection_outcome():
    submitted = []
    base = dict(aggregate_risk_budget=True)
    off = run_entry_cycle(BotState(open_positions=_blocking_book()),
                          _oc_deps(S2bFeatures(**base), submitted), MON)
    on = run_entry_cycle(BotState(open_positions=_blocking_book()),
                         _oc_deps(S2bFeatures(enable_five_wide_shadow=True, **base), submitted), MON)
    assert off[1] == on[1]
    assert len(off[0].open_positions) == len(on[0].open_positions)
