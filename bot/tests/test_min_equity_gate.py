"""Explicit capital gate (advisor nextsteps2 section 2, Task 7b).

Bot C's capital floor was IMPLICIT: it emerged from base_risk_pct=0.80 measured against a $5
wing's ~$425 structural loss, so it moved silently whenever either setting was retuned. Task 7
made the floor a stated number (`min_equity_to_open`); this module pins the ENFORCEMENT that
makes the number mean something.

The split was deliberate. This is the only change in the nextsteps2 release that sits on the live
entry path and can stop a trade that would otherwise happen, so it ships alone, where a
regression cannot be attributed to the fill-parser work.

WHICH EQUITY. The gate reads `risk_equity()` -- min(allocated_equity, broker_equity) -- the same
number the aggregate sizing path uses. A bot allocated a slice of a shared account (Bot B runs
--shared-account) must be gated on its allocation, not on the whole account's balance; a bot that
owns its account sees the two collapse to the same value. Falls back to broker_equity() only for
hand-built Deps that omit risk_equity; the live wiring always supplies it.
"""
from datetime import datetime

from bot.app.orchestrator import BotState, Deps, run_entry_cycle
from bot.strategy.s2b import OptionQuote
from bot.risk_gate import AccountState
from bot.features import S2bFeatures


def _chain(short_bid, long_ask):
    """A chain that yields a qualifying bull put spread: credit = 4.00 - 2.00 = 2.00 on the
    S2bConfig default 10-wide wing. Mirrors the fixture in test_daily_gate.py."""
    return [
        OptionQuote(568.0, 0.36, short_bid, short_bid + 0.10),
        OptionQuote(558.0, 0.18, long_ask - 0.10, long_ask),
    ]


def _acct(today, conc):
    return AccountState(100_000.0, 100_000.0, 0.0, conc, 0.0, {}, today)


def _deps(**over):
    base = dict(
        get_spot=lambda sym: 575.0, get_atr=lambda sym: 6.0,
        get_chain=lambda sym, exp: _chain(4.00, 2.00),
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


MONDAY = datetime(2026, 6, 15, 10, 5)


# ── the gate itself ───────────────────────────────────────────────────────────

def test_entry_blocked_when_equity_is_below_the_floor():
    """The whole point of the field. Below the stated floor, no new position is opened and the
    caller is told WHY -- a silent None would be indistinguishable from 'no candidate today'."""
    d = _deps(features=S2bFeatures(min_equity_to_open=2_000.0),
              risk_equity=lambda: 1_500.0, broker_equity=lambda: 1_500.0)
    state, info = run_entry_cycle(BotState(), d, MONDAY)
    assert info == "min_equity"
    assert state.open_positions == []


def test_entry_allowed_when_equity_exactly_equals_the_floor():
    """The floor is the minimum equity AT WHICH we still open, not the first blocked value. An
    account sitting exactly on its stated floor is funded, not underfunded."""
    d = _deps(features=S2bFeatures(min_equity_to_open=100_000.0))
    state, info = run_entry_cycle(BotState(), d, MONDAY)
    assert info == "filled"
    assert len(state.open_positions) == 1


def test_default_zero_floor_does_not_gate_anything():
    """Regression guard for every bot that has not opted in. min_equity_to_open defaults to 0.0
    and must leave the entry path byte-identical -- including for an account at zero equity, where
    the existing RiskGate, not this gate, is what refuses the trade."""
    d = _deps(features=S2bFeatures())
    state, info = run_entry_cycle(BotState(), d, MONDAY)
    assert info == "filled"
    assert len(state.open_positions) == 1


# ── which equity number the gate reads ────────────────────────────────────────

def test_gate_reads_allocated_risk_equity_not_the_whole_shared_account():
    """A shared-account bot must be gated on its ALLOCATION. A $100k account holding a $500
    allocation is not a funded bot, and reading broker_equity here would let the other occupant's
    capital authorize this bot's trades."""
    d = _deps(features=S2bFeatures(min_equity_to_open=2_000.0),
              risk_equity=lambda: 500.0, broker_equity=lambda: 100_000.0)
    state, info = run_entry_cycle(BotState(), d, MONDAY)
    assert info == "min_equity"
    assert state.open_positions == []


def test_gate_falls_back_to_broker_equity_when_risk_equity_is_absent():
    """risk_equity defaults to None on a hand-built Deps. The gate must still enforce rather than
    silently pass -- a capital floor that disappears when a field is unset is not a floor."""
    d = _deps(features=S2bFeatures(min_equity_to_open=2_000.0),
              risk_equity=None, broker_equity=lambda: 1_200.0)
    state, info = run_entry_cycle(BotState(), d, MONDAY)
    assert info == "min_equity"
    assert state.open_positions == []


# ── ordering and cost ─────────────────────────────────────────────────────────

def test_gate_does_not_query_the_broker_when_a_cheaper_gate_already_blocked():
    """Equity is a network call. The five cheap gates (halted/weekday/hours/max_open/max_entries)
    run first, so a halted bot must not spend a broker round-trip per cycle just to discover it
    was never going to trade."""
    calls = []

    def _equity():
        calls.append(1)
        return 1_500.0

    d = _deps(features=S2bFeatures(min_equity_to_open=2_000.0), risk_equity=_equity)
    state, info = run_entry_cycle(BotState(halted=True), d, MONDAY)
    assert info is None            # the halt gate owns this decision
    assert calls == [], "equity was fetched despite an earlier gate blocking the cycle"


def test_blocked_entry_is_recorded_in_the_decision_log():
    """The funnel report attributes every blocked cycle to a gate. An unlogged rejection shows up
    as an unexplained gap between cycles started and candidates evaluated."""
    rows = []
    d = _deps(features=S2bFeatures(min_equity_to_open=2_000.0, decision_logging=True),
              risk_equity=lambda: 1_500.0, trade_log=rows.append)
    run_entry_cycle(BotState(), d, MONDAY)
    decisions = [r for r in rows if r.get("event") == "DECISION"]
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "min_equity"
