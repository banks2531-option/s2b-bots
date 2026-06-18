from bot.ops.ledger import reconcile, DriftReport, position_key
from bot.strategy.manage import ManagedPosition


def _pos(short=568.0, long=558.0, expiry="2026-06-19"):
    return ManagedPosition("SPY", short, long, credit=3.0, qty=1, expiry=expiry)


def test_position_key_identity():
    assert position_key(_pos()) == ("SPY", 568.0, 558.0, "2026-06-19")


def test_reconcile_clean_match():
    p = _pos()
    r = reconcile([p], [_pos()], bot_equity=20_000.0, broker_equity=20_000.0)
    assert isinstance(r, DriftReport)
    assert r.positions_match() is True
    assert r.equity_drift == 0.0
    assert r.should_halt() is False


def test_reconcile_phantom_position_halts():
    # bot thinks it holds a position the broker does NOT have (phantom / already closed)
    r = reconcile([_pos()], [], bot_equity=20_000.0, broker_equity=20_000.0)
    assert r.positions_match() is False
    assert len(r.missing_at_broker) == 1
    assert r.should_halt() is True


def test_reconcile_untracked_broker_position_halts():
    # broker holds a position the bot is NOT tracking (lost track -> dangerous)
    r = reconcile([], [_pos()], bot_equity=20_000.0, broker_equity=20_000.0)
    assert len(r.untracked_at_broker) == 1
    assert r.should_halt() is True


def test_reconcile_equity_drift_beyond_tolerance_halts():
    p = _pos()
    r = reconcile([p], [_pos()], bot_equity=20_000.0, broker_equity=19_900.0)  # $100 drift
    assert r.equity_drift == 100.0
    assert r.should_halt(equity_tolerance=50.0) is True
