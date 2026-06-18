from bot.ops.monitor import Severity, Alert, alerts_for_cycle, should_halt_new_entries
from bot.ops.ledger import reconcile
from bot.strategy.manage import ManagedPosition, ExitResult, ExitAction


def _pos():
    return ManagedPosition("SPY", 568.0, 558.0, credit=3.0, qty=1, expiry="2026-06-19")


def test_failed_close_raises_critical_alert():
    failed = ExitResult(position=_pos(), action=ExitAction.STOP, close_status="timeout", failed=True)
    alerts = alerts_for_cycle([failed], drift_report=None)
    assert len(alerts) == 1
    assert alerts[0].severity == Severity.CRITICAL
    assert "did not fill" in alerts[0].message
    assert should_halt_new_entries(alerts) is True


def test_successful_close_no_alert():
    ok = ExitResult(position=_pos(), action=ExitAction.TAKE_PROFIT, close_status="filled", failed=False)
    alerts = alerts_for_cycle([ok], drift_report=None)
    assert alerts == []
    assert should_halt_new_entries(alerts) is False


def test_drift_report_raises_critical_alert():
    drift = reconcile([_pos()], [], bot_equity=20_000.0, broker_equity=20_000.0)  # phantom -> halt
    alerts = alerts_for_cycle([], drift_report=drift)
    assert len(alerts) == 1 and alerts[0].severity == Severity.CRITICAL
    assert should_halt_new_entries(alerts) is True


def test_clean_cycle_no_halt():
    drift = reconcile([_pos()], [_pos()], bot_equity=20_000.0, broker_equity=20_000.0)
    assert alerts_for_cycle([], drift_report=drift) == []
    assert should_halt_new_entries([]) is False
