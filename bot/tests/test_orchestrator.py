from datetime import datetime
from bot.app.orchestrator import BotState, Deps
from bot.app.orchestrator import run_reconcile_cycle, run_management_cycle, run_entry_cycle
from bot.strategy.manage import ManagedPosition
from bot.strategy.s2b import OptionQuote
from bot.risk_gate import AccountState


def test_botstate_defaults():
    s = BotState()
    assert s.open_positions == [] and s.halted is False and s.halt_reason == ""


def test_deps_is_constructible_with_callables():
    d = Deps(
        get_spot=lambda sym: 575.0, get_atr=lambda sym: 6.0,
        get_chain=lambda sym, exp: [], pick_expiry=lambda today: "2026-06-19",
        get_vix_regime=lambda: (0.5, 0.01), account_state=lambda today, conc: None,
        mark_position=lambda p: 3.0, dte_of=lambda p, today: 5,
        open_spread=lambda payload: "filled", close_spread=lambda p, a: "filled",
        broker_positions=lambda: [], broker_equity=lambda: 20_000.0,
        bot_equity=lambda: 20_000.0, alert_sink=lambda alerts: None,
    )
    assert d.base_risk_pct == 0.10 and d.account_equity == 20_000.0


def _pos():
    return ManagedPosition("SPY", 568.0, 558.0, credit=3.0, qty=1, expiry="2026-06-19")


def _deps(**over):
    base = dict(
        get_spot=lambda sym: 575.0, get_atr=lambda sym: 6.0,
        get_chain=lambda sym, exp: [], pick_expiry=lambda today: "2026-06-19",
        get_vix_regime=lambda: (0.5, 0.01), account_state=lambda today, conc: None,
        mark_position=lambda p: 3.0, dte_of=lambda p, today: 5,
        open_spread=lambda payload: "filled", close_spread=lambda p, a: "filled",
        broker_positions=lambda: [], broker_equity=lambda: 20_000.0,
        bot_equity=lambda: 20_000.0, alert_sink=lambda alerts: None,
    )
    base.update(over)
    return Deps(**base)


def test_reconcile_clean_no_halt():
    state = BotState(open_positions=[_pos()])
    d = _deps(broker_positions=lambda: [_pos()])
    state, drift = run_reconcile_cycle(state, d)
    assert state.halted is False


def test_reconcile_untracked_broker_position_halts_and_alerts():
    sent = []
    state = BotState(open_positions=[])               # bot thinks flat
    d = _deps(broker_positions=lambda: [_pos()],      # broker still holds one
              alert_sink=lambda alerts: sent.append(alerts))
    state, drift = run_reconcile_cycle(state, d)
    assert state.halted is True and "reconcile" in state.halt_reason
    assert sent and sent[0][0].message  # a critical alert was emitted


def test_management_closes_filled_position_and_removes_it():
    pos = _pos()
    state = BotState(open_positions=[pos])
    d = _deps(mark_position=lambda p: 10.0,           # past stop
              close_spread=lambda p, a: "filled")
    state, results = run_management_cycle(state, d, today="2026-06-17")
    assert len(results) == 1 and results[0].action.value == "stop"
    assert state.open_positions == []                 # filled close -> removed
    assert state.halted is False


def test_management_failed_close_keeps_position_and_halts():
    pos = _pos()
    sent = []
    state = BotState(open_positions=[pos])
    d = _deps(mark_position=lambda p: 10.0, close_spread=lambda p, a: "timeout",
              alert_sink=lambda alerts: sent.append(alerts))
    state, results = run_management_cycle(state, d, today="2026-06-17")
    assert results[0].failed is True
    assert state.open_positions == [pos]              # NOT removed (close didn't fill)
    assert state.halted is True and "close" in state.halt_reason
    assert sent  # alerted


def test_management_hold_keeps_position():
    pos = _pos()
    state = BotState(open_positions=[pos])
    d = _deps(mark_position=lambda p: 3.0, dte_of=lambda p, today: 5)  # midrange, high dte
    state, results = run_management_cycle(state, d, today="2026-06-17")
    assert results == [] and state.open_positions == [pos]


def _chain():
    return [OptionQuote(572.0, 0.45, 4.50, 4.60), OptionQuote(568.0, 0.36, 3.40, 3.50),
            OptionQuote(565.0, 0.30, 2.80, 2.90), OptionQuote(560.0, 0.22, 2.00, 2.10),
            OptionQuote(558.0, 0.18, 1.60, 1.70)]


def _acct(today, conc):
    return AccountState(20_000.0, 20_000.0, 0.0, conc, 0.0, {}, today)


def test_entry_opens_position_on_monday_after_10():
    state = BotState()
    d = _deps(get_chain=lambda sym, exp: _chain(), account_state=_acct,
              open_spread=lambda payload: "filled")
    now = datetime(2026, 6, 15, 10, 5)   # Monday 10:05
    state, info = run_entry_cycle(state, d, now)
    assert len(state.open_positions) == 1
    assert state.open_positions[0].short_strike == 568.0


def test_no_entry_on_tuesday():
    state = BotState()
    d = _deps(get_chain=lambda sym, exp: _chain(), account_state=_acct)
    state, info = run_entry_cycle(state, d, datetime(2026, 6, 16, 10, 5))  # Tuesday
    assert state.open_positions == []


def test_no_entry_before_10():
    state = BotState()
    d = _deps(get_chain=lambda sym, exp: _chain(), account_state=_acct)
    state, info = run_entry_cycle(state, d, datetime(2026, 6, 15, 9, 45))  # Mon 09:45
    assert state.open_positions == []


def test_no_entry_when_halted():
    state = BotState(halted=True, halt_reason="x")
    d = _deps(get_chain=lambda sym, exp: _chain(), account_state=_acct)
    state, info = run_entry_cycle(state, d, datetime(2026, 6, 15, 10, 5))
    assert state.open_positions == []


def test_no_entry_when_already_holding():
    state = BotState(open_positions=[_pos()])
    d = _deps(get_chain=lambda sym, exp: _chain(), account_state=_acct)
    state, info = run_entry_cycle(state, d, datetime(2026, 6, 15, 10, 5))
    assert len(state.open_positions) == 1   # unchanged, no second entry
