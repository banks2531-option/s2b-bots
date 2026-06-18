"""Dead-man monitor: turn failed closes + ledger drift into alerts and a halt signal (spec §4, §7)."""
from dataclasses import dataclass
from enum import Enum


class Severity(str, Enum):
    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"


@dataclass
class Alert:
    severity: Severity
    message: str


def alerts_for_cycle(exit_results, drift_report, equity_tolerance: float = 50.0):
    """CRITICAL alert for any close that did not fill (a stop that didn't execute) and for ledger drift."""
    alerts = []
    for r in exit_results:
        if r.failed:
            p = r.position
            alerts.append(Alert(Severity.CRITICAL,
                f"close did not fill ({r.action.value}) for {p.ticker} "
                f"{p.short_strike}/{p.long_strike}: {r.close_status}"))
    if drift_report is not None and drift_report.should_halt(equity_tolerance):
        alerts.append(Alert(Severity.CRITICAL,
            f"reconcile drift: equity={drift_report.equity_drift} "
            f"missing={len(drift_report.missing_at_broker)} "
            f"untracked={len(drift_report.untracked_at_broker)}"))
    return alerts


def should_halt_new_entries(alerts) -> bool:
    """Halt new entries if any CRITICAL alert is present."""
    return any(a.severity == Severity.CRITICAL for a in alerts)
