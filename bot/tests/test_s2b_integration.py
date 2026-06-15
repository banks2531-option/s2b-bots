from bot.strategy.s2b import S2bConfig, OptionQuote, build_spread_order, to_tradier_payload, is_entry_day
from bot.risk_gate import RiskGate, RiskConfig, AccountState
from bot.sizing import contracts_for_risk, regime_adjusted_risk_pct


def _chain():
    return [
        OptionQuote(572.0, 0.45, 4.50, 4.60),
        OptionQuote(568.0, 0.36, 3.40, 3.50),
        OptionQuote(565.0, 0.30, 2.80, 2.90),
        OptionQuote(560.0, 0.22, 2.00, 2.10),
        OptionQuote(558.0, 0.18, 1.60, 1.70),
    ]


def test_full_entry_pipeline_monday():
    assert is_entry_day("2026-06-15")
    cfg = S2bConfig()
    order = build_spread_order(spot=575.0, atr=6.0, chain=_chain(), cfg=cfg)
    assert order is not None
    # size it (calm VIX), then gate it
    risk = regime_adjusted_risk_pct(0.10, vix_pct_rank=0.5, vix_1d_change=0.01)
    order.qty = contracts_for_risk(20_000.0, order.max_loss_per_contract, risk)
    state = AccountState(20_000.0, 20_000.0, 0.0, 0, 0.0, {}, "2026-06-15")
    decision = RiskGate(RiskConfig()).is_order_allowed(order, state)
    assert decision.allowed is True
    payload = to_tradier_payload(order, expiry="2026-06-19", qty=order.qty)
    assert payload["class"] == "multileg" and payload["quantity[0]"] == order.qty
