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


def test_size_qty_probe_rounds_to_zero_rejects():
    f = S2bFeatures()
    # base qty is 1 (see test_size_qty_entry_constraint_binds); a 0.4 probe multiplier floors to 0
    assert size_qty(100_000.0, 2.0, 10.0, f, quality_multiplier=0.4) == 0


def test_size_qty_zero_or_negative_constraints_reject():
    f = S2bFeatures()
    assert size_qty(100_000.0, 10.0, 10.0, f) == 0   # credit == wing_width -> structural is 0


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
