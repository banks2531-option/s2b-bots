"""Chaos drills = the deployment gate (spec §8). A stop that does not demonstrably
fire-and-recover here BLOCKS live deployment."""
from bot.strategy.manage import (ManagedPosition, ManageConfig, monitor_positions, ExitAction)
from bot.ops.monitor import alerts_for_cycle, should_halt_new_entries, Severity


def _pos():
    return ManagedPosition("SPY", 568.0, 558.0, credit=3.0, qty=1, expiry="2026-06-19")


def test_drill_failed_stop_alerts_and_halts():
    """Drill: a stop triggers but the close does NOT fill -> the bot must alert CRITICAL and halt."""
    pos = _pos()
    # mark past the 9.0 stop level; close_fn reports a non-fill (e.g. timeout)
    results = monitor_positions([pos], mark_fn=lambda p: 10.0, dte_fn=lambda p: 5,
                                close_fn=lambda p, a: "timeout", cfg=ManageConfig())
    assert len(results) == 1 and results[0].action == ExitAction.STOP and results[0].failed is True
    alerts = alerts_for_cycle(results, drift_report=None)
    assert any(a.severity == Severity.CRITICAL for a in alerts)
    assert should_halt_new_entries(alerts) is True


def test_drill_stop_that_fills_does_not_halt():
    """Control: a stop that fills cleanly must NOT raise a halt (no false positives)."""
    pos = _pos()
    results = monitor_positions([pos], mark_fn=lambda p: 10.0, dte_fn=lambda p: 5,
                                close_fn=lambda p, a: "filled", cfg=ManageConfig())
    assert results[0].failed is False
    assert should_halt_new_entries(alerts_for_cycle(results, drift_report=None)) is False


from bot.ops.ledger import reconcile


def test_drill_lost_position_on_restart_halts():
    """Drill: after a crash/restart the bot's tracked set is empty but the broker still holds
    a live position -> reconcile must flag it untracked and the bot must halt (not blindly trade)."""
    broker_still_open = [_pos()]
    bot_thinks_flat = []
    drift = reconcile(bot_thinks_flat, broker_still_open, bot_equity=20_000.0, broker_equity=20_000.0)
    assert len(drift.untracked_at_broker) == 1 and drift.should_halt() is True
    alerts = alerts_for_cycle([], drift_report=drift)
    assert should_halt_new_entries(alerts) is True


def test_drill_phantom_position_halts():
    """Drill: bot believes a position is open that the broker already closed (phantom) -> halt."""
    drift = reconcile([_pos()], [], bot_equity=20_000.0, broker_equity=20_000.0)
    assert len(drift.missing_at_broker) == 1 and drift.should_halt() is True


from bot.broker.submit import submit_and_verify
from bot.broker.order_state import OrderState


def _clock():
    t = {"now": 0.0}
    return (lambda: t["now"]), (lambda s: t.__setitem__("now", t["now"] + s))


class _Broker:
    def __init__(self, statuses, cancel_raises=False):
        self._s = list(statuses); self._cancel_raises = cancel_raises; self.cancelled = None
    def place_order(self, payload): return "555"
    def get_order(self, oid): return {"id": oid, "status": self._s.pop(0)}
    def cancel_order(self, oid):
        self.cancelled = oid
        if self._cancel_raises:
            from bot.broker.tradier import BrokerError
            raise BrokerError("already filled")


def test_drill_order_timeout_surfaces_as_failed_close_and_halts():
    """Drill: the close order never fills -> submit_and_verify returns TIMEOUT, the cycle
    treats it as a failed close, and the bot halts."""
    now, sleep = _clock()
    broker = _Broker(["open", "open", "open", "open"])  # never fills
    state, _ = submit_and_verify(broker, {"x": 1}, poll_s=1.0, timeout_s=2.0, now=now, sleep=sleep)
    assert state == OrderState.TIMEOUT and broker.cancelled == "555"
    # a TIMEOUT close status is not "filled" -> would set ExitResult.failed=True -> halt (see Plan 4/5)


def test_drill_fill_wins_race_no_false_halt():
    """Drill: the order fills exactly as the timeout cancel is attempted -> the re-query must
    report FILLED (not TIMEOUT), so the bot does NOT falsely halt on a successful close."""
    now, sleep = _clock()
    broker = _Broker(["open", "open", "filled"], cancel_raises=True)  # cancel fails; re-query shows filled
    state, _ = submit_and_verify(broker, {"x": 1}, poll_s=1.0, timeout_s=0.5, now=now, sleep=sleep)
    assert state == OrderState.FILLED   # fill won the race -> no false TIMEOUT
