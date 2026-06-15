from bot.strategy.manage import ExitAction, ManageConfig, decide_exit

# credit = 3.0; stop level = 3.0*(1+2.0)=9.0; tp level = 3.0*(1-0.5)=1.5
CFG = ManageConfig(tp_pct=0.50, stop_mult=2.0, time_exit_dte=1)


def test_stop_fires_at_and_above_level():
    assert decide_exit(current_value=9.0, credit=3.0, dte=5, cfg=CFG) == ExitAction.STOP
    assert decide_exit(current_value=12.0, credit=3.0, dte=5, cfg=CFG) == ExitAction.STOP


def test_take_profit_at_and_below_level():
    assert decide_exit(current_value=1.5, credit=3.0, dte=5, cfg=CFG) == ExitAction.TAKE_PROFIT
    assert decide_exit(current_value=0.8, credit=3.0, dte=5, cfg=CFG) == ExitAction.TAKE_PROFIT


def test_time_exit_when_dte_low_and_midrange():
    assert decide_exit(current_value=3.0, credit=3.0, dte=1, cfg=CFG) == ExitAction.TIME_EXIT


def test_hold_midrange_high_dte():
    assert decide_exit(current_value=3.0, credit=3.0, dte=5, cfg=CFG) == ExitAction.HOLD


def test_stop_takes_priority_over_time_exit():
    # at a loss past the stop AND near expiry -> STOP wins (risk first)
    assert decide_exit(current_value=10.0, credit=3.0, dte=1, cfg=CFG) == ExitAction.STOP


from bot.strategy.manage import ManagedPosition, spread_value_mid, dte_from_expiry
from bot.strategy.s2b import OptionQuote


def test_spread_value_mid_cost_to_close():
    # bull put: short higher strike, long lower. value = short_mid - long_mid
    short_q = OptionQuote(strike=568.0, delta=0.36, bid=3.40, ask=3.60)   # mid 3.50
    long_q = OptionQuote(strike=558.0, delta=0.18, bid=1.60, ask=1.80)    # mid 1.70
    assert spread_value_mid(short_q, long_q) == 1.80   # 3.50 - 1.70


def test_dte_from_expiry():
    assert dte_from_expiry("2026-06-19", today="2026-06-15") == 4
    assert dte_from_expiry("2026-06-15", today="2026-06-15") == 0


from bot.strategy.manage import build_close_payload


def test_build_close_payload_buys_back_short_sells_long():
    pos = ManagedPosition(ticker="SPY", short_strike=568.0, long_strike=558.0,
                          credit=3.0, qty=2, expiry="2026-06-19")
    payload = build_close_payload(pos, limit_price=1.20)
    assert payload["class"] == "multileg"
    assert payload["type"] == "debit"      # closing a credit spread costs a debit
    assert payload["price"] == 1.20
    assert payload["option_symbol[0]"] == "SPY260619P00568000"
    assert payload["side[0]"] == "buy_to_close" and payload["quantity[0]"] == 2
    assert payload["option_symbol[1]"] == "SPY260619P00558000"
    assert payload["side[1]"] == "sell_to_close" and payload["quantity[1]"] == 2


from bot.strategy.manage import monitor_positions, ExitResult


def _pos(**kw):
    base = dict(ticker="SPY", short_strike=568.0, long_strike=558.0, credit=3.0, qty=1, expiry="2026-06-19")
    base.update(kw)
    return ManagedPosition(**base)


def test_monitor_fires_stop_and_records_filled():
    pos = _pos()
    marks = {id(pos): 10.0}    # past stop (>=9.0)
    closes = []

    def mark_fn(p): return marks[id(p)]
    def dte_fn(p): return 5
    def close_fn(p, action): closes.append((p, action)); return "filled"

    results = monitor_positions([pos], mark_fn, dte_fn, close_fn, CFG)
    assert len(results) == 1
    r = results[0]
    assert isinstance(r, ExitResult)
    assert r.action == ExitAction.STOP and r.close_status == "filled" and r.failed is False
    assert closes == [(pos, ExitAction.STOP)]


def test_monitor_holds_when_no_exit():
    pos = _pos()
    results = monitor_positions([pos], lambda p: 3.0, lambda p: 5, lambda p, a: "filled", CFG)
    assert results == []   # HOLD -> no close attempted


def test_monitor_flags_failed_close():
    # a stop that does NOT reach 'filled' must be surfaced as failed (the bot-B lesson)
    pos = _pos()
    results = monitor_positions([pos], lambda p: 10.0, lambda p: 5,
                                lambda p, a: "timeout", CFG)
    assert len(results) == 1
    assert results[0].failed is True and results[0].close_status == "timeout"
