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
