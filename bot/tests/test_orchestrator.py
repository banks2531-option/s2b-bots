from bot.app.orchestrator import BotState, Deps
from bot.app.orchestrator import run_reconcile_cycle
from bot.strategy.manage import ManagedPosition


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
