from bot.risk_gate import RiskGate, RiskConfig, SpreadOrder, AccountState


def _ok_order(**kw):
    base = dict(ticker="SPY", structure="bull_put_spread", short_strike=560.0,
                long_strike=550.0, credit=3.0, spot=575.0, atr=6.0,
                max_loss_per_contract=700.0, qty=2)
    base.update(kw)
    return SpreadOrder(**base)


def _ok_state(**kw):
    base = dict(equity=20_000.0, settled_cash=20_000.0, open_risk=0.0,
                concurrent_positions=0, realized_pnl_today=0.0,
                recent_losses={}, current_date="2026-06-15")
    base.update(kw)
    return AccountState(**base)


def test_allowed_structure_passes_structure_check():
    gate = RiskGate(RiskConfig())
    assert gate.is_order_allowed(_ok_order(), _ok_state()).allowed is True


def test_directional_debit_spread_rejected():
    gate = RiskGate(RiskConfig())
    d = gate.is_order_allowed(_ok_order(structure="bull_call_spread"), _ok_state())
    assert d.allowed is False
    assert "structure" in d.reason
