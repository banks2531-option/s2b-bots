from bot.app.wiring import reconcile_live
from bot.strategy.manage import ManagedPosition


def _pos(qty=2):
    return ManagedPosition("SPY", 568.0, 558.0, credit=3.0, qty=qty, expiry="2026-06-19")


def test_reconcile_live_clean_match():
    legs = {"SPY260619P00568000": -2, "SPY260619P00558000": 2}   # short -2, long +2
    r = reconcile_live([_pos(2)], legs, bot_equity=20_000.0, broker_equity=20_000.0)
    assert r.should_halt() is False


def test_reconcile_live_qty_mismatch_halts():
    legs = {"SPY260619P00568000": -1, "SPY260619P00558000": 1}   # broker only 1 lot, bot tracks 2
    r = reconcile_live([_pos(2)], legs, bot_equity=20_000.0, broker_equity=20_000.0)
    assert r.should_halt() is True


def test_reconcile_live_phantom_halts():
    r = reconcile_live([_pos(2)], {}, bot_equity=20_000.0, broker_equity=20_000.0)  # broker flat
    assert len(r.missing_at_broker) == 1 and r.should_halt() is True


def test_reconcile_live_untracked_leg_halts():
    legs = {"SPY260619P00568000": -2, "SPY260619P00558000": 2, "AAPL260619P00250000": -3}
    r = reconcile_live([_pos(2)], legs, bot_equity=20_000.0, broker_equity=20_000.0)
    assert len(r.untracked_at_broker) == 1 and r.should_halt() is True
