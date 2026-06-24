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


def test_reconcile_qty_mismatch_halts():
    """C1 CRITICAL: bot tracks qty=2 but broker holds qty=1 for the same key
    -> double-exposure must be detected as a qty mismatch and trigger a halt."""
    bot_pos = ManagedPosition("SPY", 568.0, 558.0, credit=3.0, qty=2, expiry="2026-06-19")
    brk_pos = ManagedPosition("SPY", 568.0, 558.0, credit=3.0, qty=1, expiry="2026-06-19")
    r = reconcile([bot_pos], [brk_pos], bot_equity=20_000.0, broker_equity=20_000.0)
    assert len(r.qty_mismatch) == 1
    assert r.positions_match() is False
    assert r.should_halt() is True


# ── shared-account mode (multiple bots on ONE account) ─────────────────────────

def _p(short, long, qty, expiry="2026-06-19"):
    return ManagedPosition("SPY", short, long, credit=3.0, qty=qty, expiry=expiry)


def test_shared_account_ignores_untracked_position():
    # bot flat; broker holds another bot's spread -> with ignore_untracked, no halt
    other = _p(560.0, 550.0, 2)
    r = reconcile([], [other], 20_000.0, 20_000.0, ignore_untracked=True, qty_at_least=True)
    assert r.untracked_at_broker == [] and r.should_halt() is False


def test_shared_account_broker_has_more_is_ok():
    # bot tracks qty 2; broker shows qty 4 (this bot's 2 + another bot's 2) -> at_least passes
    bot = _p(568.0, 558.0, 2)
    brk = _p(568.0, 558.0, 4)
    r = reconcile([bot], [brk], 20_000.0, 20_000.0, ignore_untracked=True, qty_at_least=True)
    assert r.qty_mismatch == [] and r.should_halt() is False


def test_shared_account_broker_has_less_halts():
    # this bot's own position shrank below tracked qty -> still a real problem -> halt
    bot = _p(568.0, 558.0, 2)
    brk = _p(568.0, 558.0, 1)
    r = reconcile([bot], [brk], 20_000.0, 20_000.0, ignore_untracked=True, qty_at_least=True)
    assert len(r.qty_mismatch) == 1 and r.should_halt() is True


def test_shared_account_still_halts_on_own_phantom():
    # bot's tracked position is entirely gone at the broker -> halt even in shared mode
    bot = _p(568.0, 558.0, 2)
    r = reconcile([bot], [], 20_000.0, 20_000.0, ignore_untracked=True, qty_at_least=True)
    assert len(r.missing_at_broker) == 1 and r.should_halt() is True
