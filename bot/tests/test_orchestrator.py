from bot.app.orchestrator import BotState, Deps


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
