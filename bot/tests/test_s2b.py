from bot.strategy.s2b import is_entry_day


def test_monday_is_entry_day():
    assert is_entry_day("2026-06-15") is True   # Monday


def test_other_days_not_entry():
    assert is_entry_day("2026-06-16") is False   # Tuesday
    assert is_entry_day("2026-06-19") is False   # Friday
from bot.strategy.s2b import OptionQuote, S2bConfig, select_short_put


def _chain():
    # SPY puts; abs delta; strike, delta, bid, ask. spot assumed 575, ATR 6.
    return [
        OptionQuote(strike=572.0, delta=0.45, bid=4.50, ask=4.60),  # cushion 0.5 ATR -> excluded
        OptionQuote(strike=568.0, delta=0.36, bid=3.40, ask=3.50),  # cushion 1.17 ATR, delta near 0.35
        OptionQuote(strike=565.0, delta=0.30, bid=2.80, ask=2.90),  # cushion 1.67 ATR
        OptionQuote(strike=560.0, delta=0.22, bid=2.00, ask=2.10),  # cushion 2.5 ATR
    ]


def test_select_short_put_respects_cushion_and_delta():
    cfg = S2bConfig(target_delta=0.35, wing_width=10.0, min_cushion_atr=1.0)
    pick = select_short_put(_chain(), spot=575.0, atr=6.0, cfg=cfg)
    # 572 (delta 0.45, closest to nothing) is excluded by cushion; among cushion-OK,
    # 568 (delta 0.36) is closest to target 0.35
    assert pick.strike == 568.0


def test_select_short_put_none_when_no_cushion():
    cfg = S2bConfig(target_delta=0.35, wing_width=10.0, min_cushion_atr=5.0)  # demand 5 ATR
    assert select_short_put(_chain(), spot=575.0, atr=6.0, cfg=cfg) is None
from bot.strategy.s2b import build_spread_order
from bot.risk_gate import SpreadOrder


def test_build_spread_order_builds_bull_put():
    cfg = S2bConfig(target_delta=0.35, wing_width=10.0, min_cushion_atr=1.0)
    # add the long wing (568 short -> 558 long) to the chain
    chain = _chain() + [OptionQuote(strike=558.0, delta=0.18, bid=1.60, ask=1.70)]
    order = build_spread_order(spot=575.0, atr=6.0, chain=chain, cfg=cfg)
    assert isinstance(order, SpreadOrder)
    assert order.structure == "bull_put_spread"
    assert order.short_strike == 568.0 and order.long_strike == 558.0
    # credit = short.bid - long.ask = 3.40 - 1.70 = 1.70
    assert order.credit == 1.70
    # max loss per contract = (10 - 1.70) * 100 = 830
    assert order.max_loss_per_contract == 830.0
    assert order.qty == 1   # sizing sets real qty later


def test_build_spread_order_none_when_long_wing_missing():
    cfg = S2bConfig(target_delta=0.35, wing_width=10.0, min_cushion_atr=1.0)
    order = build_spread_order(spot=575.0, atr=6.0, chain=_chain(), cfg=cfg)  # no 558 strike
    assert order is None


def test_build_spread_order_none_when_nonpositive_credit():
    cfg = S2bConfig(target_delta=0.35, wing_width=10.0, min_cushion_atr=1.0)
    # long wing priced higher than short bid -> credit <= 0
    chain = _chain() + [OptionQuote(strike=558.0, delta=0.18, bid=3.50, ask=3.60)]
    assert build_spread_order(spot=575.0, atr=6.0, chain=chain, cfg=cfg) is None
from bot.strategy.s2b import to_tradier_payload


def test_to_tradier_payload_multileg_bull_put():
    cfg = S2bConfig(target_delta=0.35, wing_width=10.0, min_cushion_atr=1.0)
    chain = _chain() + [OptionQuote(strike=558.0, delta=0.18, bid=1.60, ask=1.70)]
    order = build_spread_order(spot=575.0, atr=6.0, chain=chain, cfg=cfg)
    payload = to_tradier_payload(order, expiry="2026-06-19", qty=2)
    assert payload["class"] == "multileg"
    assert payload["symbol"] == "SPY"
    assert payload["type"] == "credit"
    assert payload["duration"] == "day"
    # short put leg = sell to open; long put leg = buy to open
    assert payload["option_symbol[0]"] == "SPY260619P00568000"
    assert payload["side[0]"] == "sell_to_open" and payload["quantity[0]"] == 2
    assert payload["option_symbol[1]"] == "SPY260619P00558000"
    assert payload["side[1]"] == "buy_to_open" and payload["quantity[1]"] == 2
