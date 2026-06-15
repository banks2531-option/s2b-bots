from bot.sizing import contracts_for_risk, regime_adjusted_risk_pct
from bot.risk_gate import RiskGate, RiskConfig, SpreadOrder, AccountState


def test_size_then_gate_happy_path():
    equity = 20_000.0
    risk = regime_adjusted_risk_pct(0.10, vix_pct_rank=0.5, vix_1d_change=0.01)  # calm -> 0.10
    qty = contracts_for_risk(equity, max_loss_per_contract=700.0, risk_pct=risk)  # -> 2
    order = SpreadOrder("SPY", "bull_put_spread", 560.0, 550.0, 3.0, 575.0, 6.0, 700.0, qty)
    state = AccountState(equity, equity, 0.0, 0, 0.0, {}, "2026-06-15")
    assert RiskGate(RiskConfig()).is_order_allowed(order, state).allowed is True


def test_vix_spike_downsizes_and_still_passes():
    equity = 20_000.0
    risk = regime_adjusted_risk_pct(0.10, vix_pct_rank=0.9, vix_1d_change=0.0)  # elevated -> 0.05
    qty = contracts_for_risk(equity, 700.0, risk)  # floor(1000/700)=1
    assert qty == 1
    order = SpreadOrder("SPY", "bull_put_spread", 560.0, 550.0, 3.0, 575.0, 6.0, 700.0, qty)
    state = AccountState(equity, equity, 0.0, 0, 0.0, {}, "2026-06-15")
    assert RiskGate(RiskConfig()).is_order_allowed(order, state).allowed is True
