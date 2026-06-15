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


from bot.risk_gate import RiskConfig as _RC  # alias to build tight configs


def test_bull_put_thin_cushion_rejected():
    # spot 575, short 572, ATR 6 -> cushion 0.5 ATR < 1.0 -> reject
    gate = RiskGate(RiskConfig())
    d = gate.is_order_allowed(_ok_order(short_strike=572.0), _ok_state())
    assert d.allowed is False
    assert "cushion" in d.reason


def test_bull_put_fat_cushion_allowed():
    # spot 575, short 560, ATR 6 -> cushion 2.5 ATR -> ok
    gate = RiskGate(RiskConfig())
    assert gate.is_order_allowed(_ok_order(short_strike=560.0), _ok_state()).allowed is True


def test_bear_call_cushion_uses_other_side():
    # bear_call: cushion = (short - spot)/atr; short 590, spot 575, atr 6 -> 2.5 ATR ok
    gate = RiskGate(RiskConfig())
    o = _ok_order(structure="bear_call_spread", short_strike=590.0, long_strike=600.0)
    assert gate.is_order_allowed(o, _ok_state()).allowed is True


def test_per_trade_risk_cap_rejects():
    # 10% of $20k = $2000 cap; order risk = 700*4 = $2800 -> reject
    gate = RiskGate(RiskConfig())
    d = gate.is_order_allowed(_ok_order(qty=4), _ok_state())
    assert d.allowed is False
    assert "per-trade risk" in d.reason


def test_total_open_risk_cap_rejects():
    # 30% of $20k = $6000 total cap; already $5000 open + $1400 new = $6400 -> reject
    gate = RiskGate(RiskConfig())
    d = gate.is_order_allowed(_ok_order(qty=2), _ok_state(open_risk=5000.0))
    assert d.allowed is False
    assert "total open risk" in d.reason


def test_max_concurrent_rejects():
    gate = RiskGate(RiskConfig())
    d = gate.is_order_allowed(_ok_order(), _ok_state(concurrent_positions=3))
    assert d.allowed is False and "concurrent" in d.reason


def test_daily_loss_halt_rejects():
    # 2% of $20k = $400 loss halt; today -$450 -> reject
    gate = RiskGate(RiskConfig())
    d = gate.is_order_allowed(_ok_order(), _ok_state(realized_pnl_today=-450.0))
    assert d.allowed is False and "daily loss" in d.reason


def test_insufficient_settled_cash_rejects():
    # trade risk 700*2=1400 but only $1000 settled -> reject (cash account)
    gate = RiskGate(RiskConfig())
    d = gate.is_order_allowed(_ok_order(qty=2), _ok_state(settled_cash=1000.0))
    assert d.allowed is False and "settled cash" in d.reason


def test_same_ticker_cooldown_rejects():
    gate = RiskGate(RiskConfig())
    d = gate.is_order_allowed(_ok_order(ticker="SPY"), _ok_state(recent_losses={"SPY": 2}))
    assert d.allowed is False and "cooldown" in d.reason


def test_cooldown_expired_allows():
    gate = RiskGate(RiskConfig())
    d = gate.is_order_allowed(_ok_order(ticker="SPY"), _ok_state(recent_losses={"SPY": 5}))
    assert d.allowed is True
