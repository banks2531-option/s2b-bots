"""Continuous daily-risk gate (partner review v2 §11): don't wait for the first stop to
restrict entries. Tracks realized P&L for the day and halts/reduces new entries when the
day's total risk consumption (realized loss + remaining stop risk of today's entries +
the proposed trade's stop risk) exceeds budget, or when today's total P&L (realized +
unrealized) breaches the daily loss-halt threshold. Management/closing always keeps running."""
from datetime import datetime

import pytest

from bot.app.orchestrator import BotState, Deps, run_entry_cycle, run_management_cycle
from bot.strategy.manage import ManagedPosition
from bot.strategy.s2b import OptionQuote
from bot.risk_gate import AccountState
from bot.features import S2bFeatures


# ── realized_today accumulation across closes, reset on a new day ──────────────

def _pos(short, long_, credit, qty, expiry="2026-07-18", entry_date="2026-07-10"):
    return ManagedPosition("SPY", short, long_, credit, qty, expiry, entry_date=entry_date)


def position_id(p):
    return (p.short_strike, p.long_strike, p.expiry)


def _manage_deps(mark_map, close_status="filled", dte=0):
    def mark_fn(p):
        return mark_map[position_id(p)]

    def close_fn(p, action):
        return close_status

    return Deps(
        get_spot=lambda sym: 575.0, get_atr=lambda sym: 6.0,
        get_chain=lambda sym, exp: [], pick_expiry=lambda today: "2026-07-18",
        get_vix_regime=lambda: (0.5, 0.01),
        account_state=lambda today, conc: AccountState(100_000.0, 100_000.0, 0.0, conc, 0.0, {}, today),
        mark_position=mark_fn, dte_of=lambda p, today: dte,   # 0 DTE -> TIME_EXIT triggers a close
        open_spread=lambda payload: "filled", close_spread=close_fn,
        broker_positions=lambda: [], broker_equity=lambda: 100_000.0,
        bot_equity=lambda: 100_000.0, alert_sink=lambda alerts: None,
        risk_equity=lambda: 100_000.0,
    )


def test_realized_today_accumulates_across_same_day_closes():
    p1 = _pos(568.0, 558.0, 2.0, 1, expiry="2026-07-18")
    p2 = _pos(560.0, 550.0, 1.5, 2, expiry="2026-07-19")
    state = BotState(open_positions=[p1, p2])
    mark_map = {position_id(p1): 3.0, position_id(p2): 4.0}   # both TIME_EXIT (0 DTE) at these marks
    deps = _manage_deps(mark_map)
    state, results = run_management_cycle(state, deps, "2026-07-10")
    assert state.risk_day == "2026-07-10"
    # p1 pnl = (2.0-3.0)*100*1 = -100; p2 pnl = (1.5-4.0)*100*2 = -500 -> total -600
    assert state.realized_today == pytest.approx(-600.0)


def test_realized_today_resets_on_a_new_day():
    p1 = _pos(568.0, 558.0, 2.0, 1, expiry="2026-07-18")
    state = BotState(open_positions=[p1], realized_today=-999.0, risk_day="2026-07-09")
    mark_map = {position_id(p1): 1.0}   # TIME_EXIT, pnl = (2.0-1.0)*100*1 = +100
    deps = _manage_deps(mark_map)
    state, results = run_management_cycle(state, deps, "2026-07-10")
    assert state.risk_day == "2026-07-10"
    # the stale -999.0 from the PRIOR day must be dropped before today's +100 is added
    assert state.realized_today == pytest.approx(100.0)


def test_realized_today_unchanged_when_no_close_happens():
    p1 = _pos(568.0, 558.0, 2.0, 1, expiry="2026-07-18")
    state = BotState(open_positions=[p1], realized_today=50.0, risk_day="2026-07-10")
    mark_map = {position_id(p1): 2.0}   # at credit -> HOLD (no exit)
    deps = _manage_deps(mark_map, dte=5)   # plenty of DTE left -> no TIME_EXIT
    state, results = run_management_cycle(state, deps, "2026-07-10")
    assert results == []
    assert state.realized_today == pytest.approx(50.0)   # untouched: same day, no close


# ── run_entry_cycle wired behind deps.features.aggregate_risk_budget ───────────

def _chain(short_bid, long_ask):
    return [
        OptionQuote(568.0, 0.36, short_bid, short_bid + 0.10),
        OptionQuote(558.0, 0.18, long_ask - 0.10, long_ask),
    ]


def _acct(today, conc):
    return AccountState(100_000.0, 100_000.0, 0.0, conc, 0.0, {}, today)


def _deps(**over):
    base = dict(
        get_spot=lambda sym: 575.0, get_atr=lambda sym: 6.0,
        get_chain=lambda sym, exp: _chain(4.00, 2.00),   # credit=2.00, wing=10 (S2bConfig default)
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
TODAY = "2026-06-15"


def test_daily_gate_reduces_qty_when_realized_loss_pushes_over_same_day_budget():
    # req=1,000,000 -> pre-gate (1.4 aggregate sizing + 1.5 gap-stress) qty = 18 (no existing book,
    # credit=2.0/wing=10 -> per-contract stop = planned_stop_loss_per_contract = 410).
    # A same-day realized loss of -15,000 already eats into the 2% same-day stop budget (20,000):
    # daily_risk_consumption(qty) = 15000 + 0 (no today-entries in the book) + 410*qty.
    # 15000 + 410*18 = 22380 > 20000 -> must shrink; 15000 + 410*12 = 19920 <= 20000 (13 -> 20330 fails).
    state = BotState(realized_today=-15_000.0, risk_day=TODAY)
    f = S2bFeatures(aggregate_risk_budget=True)
    d = _deps(features=f, risk_equity=lambda: 1_000_000.0)
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    assert len(state.open_positions) == 1
    assert state.open_positions[0].qty == 12


def test_daily_gate_rejects_when_even_one_contract_breaches_same_day_budget():
    # Same setup, but realized loss (-25,000) alone already exceeds the 20,000 same-day budget --
    # even qty=1 (410) can't fit: 25000+410=25410 > 20000 -> reject outright.
    state = BotState(realized_today=-25_000.0, risk_day=TODAY)
    f = S2bFeatures(aggregate_risk_budget=True)
    d = _deps(features=f, risk_equity=lambda: 1_000_000.0)
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "daily_risk"
    assert state.open_positions == []


def test_daily_gate_halts_new_entries_on_day_pnl_breach_but_daily_risk_check_still_passes():
    # req=100_000 (default) -> pre-gate qty=1 (structural/entry-stop binds at 1 regardless of the
    # daily gate). A small same-day realized loss (-100) plus proposed_stop (410) is well within the
    # 2000 same-day budget, so the daily_risk_consumption check passes cleanly (no reduction/reject).
    # But an existing position opened YESTERDAY (so it does NOT count toward today_stop) is deep
    # underwater: unrealized = (2.0-6.0)*100*5 = -2000. realized_today(-100) + unrealized(-2000) =
    # -2100 <= -0.02*100_000 (-2000) -> day-loss halt fires, blocking the new entry.
    existing = _pos(568.0, 558.0, 2.0, 5, expiry="2026-07-18", entry_date="2026-06-14")
    state = BotState(open_positions=[existing], realized_today=-100.0, risk_day=TODAY)
    f = S2bFeatures(aggregate_risk_budget=True)
    d = _deps(features=f, mark_position=lambda p: 6.0, max_open=2)   # allow an entry attempt
                                                                      # despite the existing position
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "day_loss_halt"
    assert state.open_positions == [existing]   # no new position added; existing left untouched


def test_daily_gate_management_still_runs_during_a_day_loss_halt():
    # A halted day for NEW entries must not stop stops/closes from firing (spec §11).
    p = _pos(568.0, 558.0, 2.0, 1, expiry="2026-07-18")
    state = BotState(open_positions=[p])
    mark_map = {position_id(p): 3.0}   # TIME_EXIT (0 DTE) -> a close fires regardless of any halt
    deps = _manage_deps(mark_map)
    state, results = run_management_cycle(state, deps, "2026-07-10")
    assert len(results) == 1
    assert state.open_positions == []
    assert state.realized_today == pytest.approx(-100.0)


def test_daily_gate_off_by_default_unchanged():
    state = BotState()
    d = _deps()   # features defaults -> aggregate_risk_budget False
    assert d.features.aggregate_risk_budget is False
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    assert len(state.open_positions) == 1
    # legacy contracts_for_risk sizing path, untouched by the daily gate
    assert state.open_positions[0].qty == 12


def test_daily_gate_resets_realized_today_on_a_new_calendar_day():
    # stale realized_today from a PRIOR day must not count against today's budget.
    state = BotState(realized_today=-1_000_000.0, risk_day="2026-06-01")
    f = S2bFeatures(aggregate_risk_budget=True)
    d = _deps(features=f)
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    assert state.risk_day == TODAY
    assert state.realized_today == 0.0
    assert state.open_positions[0].qty == 1
