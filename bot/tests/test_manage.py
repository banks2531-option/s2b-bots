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
