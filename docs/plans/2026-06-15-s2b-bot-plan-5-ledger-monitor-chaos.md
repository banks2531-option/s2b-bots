# S2b Bot — Plan 5: Truth Ledger + Dead-Man Monitor + Chaos Drills (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Close the safety loop. (1) A truth ledger that reconciles bot-tracked state against broker truth and demands a halt on any drift (the C2 lesson: drift > $50 → halt). (2) A dead-man monitor that turns Plan-4's `failed`-close flag and ledger drift into CRITICAL alerts and a halt-new-entries signal. (3) Chaos drills — the deployment gate — proving a failed stop, a lost position, and an order timeout each ALERT and HALT (spec §8).

**Architecture:** `bot/ops/ledger.py` = pure `reconcile(bot_positions, broker_positions, bot_equity, broker_equity) -> DriftReport` (position-set + equity comparison, `should_halt`). `bot/ops/monitor.py` = `alerts_for_cycle` (Plan-4 `ExitResult.failed` + ledger drift → `Alert`s) and `should_halt_new_entries`. The chaos drills are integration TESTS that drive the real Plan-2/Plan-4 machinery through three failure scenarios and assert alert+halt — these tests ARE the deployment gate.

**Tech Stack:** Python 3.11, pytest, dataclasses/enum (stdlib). Reuses `bot.strategy.manage` (monitor_positions, ManagedPosition, ExitResult) and `bot.broker.submit.submit_and_verify`.

**Spec:** `docs/specs/2026-06-15-s2b-execution-bot-design.md` §4 (dead-man on the failed-close flag), §7 (truth ledger, drift > $50 → halt; phantom/duplicate-close guards), §8 (chaos drills gate live deployment).

---

### Task 1: ops package + reconcile → DriftReport

**Files:** Create `bot/ops/__init__.py`, `bot/ops/ledger.py`, `bot/tests/test_ledger.py`

- [ ] **Step 1: Write the failing test** — `bot/tests/test_ledger.py`:
```python
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
```

- [ ] **Step 2: Run, expect fail** — `ModuleNotFoundError: bot.ops.ledger`.

- [ ] **Step 3: Implement** — `bot/ops/__init__.py`:
```python
"""Operations: truth ledger, monitoring, chaos drills."""
```
`bot/ops/ledger.py`:
```python
"""Truth ledger: reconcile bot-tracked state against broker truth (spec §7, C2)."""
from dataclasses import dataclass


def position_key(p):
    """Stable identity of a spread position."""
    return (p.ticker, p.short_strike, p.long_strike, p.expiry)


@dataclass
class DriftReport:
    missing_at_broker: list    # bot thinks open, broker has not (phantom/already-closed)
    untracked_at_broker: list  # broker holds, bot is not tracking (lost track)
    equity_drift: float        # bot_equity - broker_equity

    def positions_match(self) -> bool:
        return not self.missing_at_broker and not self.untracked_at_broker

    def should_halt(self, equity_tolerance: float = 50.0) -> bool:
        return (not self.positions_match()) or abs(self.equity_drift) > equity_tolerance


def reconcile(bot_positions, broker_positions, bot_equity, broker_equity) -> DriftReport:
    bot_keys = {position_key(p): p for p in bot_positions}
    brk_keys = {position_key(p): p for p in broker_positions}
    missing = [bot_keys[k] for k in bot_keys if k not in brk_keys]
    untracked = [brk_keys[k] for k in brk_keys if k not in bot_keys]
    return DriftReport(missing_at_broker=missing, untracked_at_broker=untracked,
                       equity_drift=round(bot_equity - broker_equity, 2))
```

- [ ] **Step 4: Run, expect pass** — `python -m pytest bot/tests/test_ledger.py -v` → 5 passed.

- [ ] **Step 5: Commit**
```bash
git add bot/ops/__init__.py bot/ops/ledger.py bot/tests/test_ledger.py
git commit -m "feat: truth-ledger reconcile (phantom/untracked/equity drift -> halt)"
```

---

### Task 2: Dead-man monitor — alerts + halt signal

**Files:** Create `bot/ops/monitor.py`, `bot/tests/test_ops_monitor.py`

- [ ] **Step 1: Write the failing test** — `bot/tests/test_ops_monitor.py`:
```python
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
```

- [ ] **Step 2: Run, expect fail** — `ModuleNotFoundError: bot.ops.monitor`.

- [ ] **Step 3: Implement** — `bot/ops/monitor.py`:
```python
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
```

- [ ] **Step 4: Run, expect pass** — `python -m pytest bot/tests/test_ops_monitor.py -v` → 4 passed.

- [ ] **Step 5: Commit**
```bash
git add bot/ops/monitor.py bot/tests/test_ops_monitor.py
git commit -m "feat: dead-man monitor (failed close + drift -> critical alert + halt)"
```

---

### Task 3: Chaos drill — a failed stop ALERTS and HALTS (the deployment gate)

**Files:** Create `bot/tests/test_chaos_drills.py`

- [ ] **Step 1: Write the drill** — `bot/tests/test_chaos_drills.py`:
```python
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
```

- [ ] **Step 2: Run, expect pass** — `python -m pytest bot/tests/test_chaos_drills.py -v` → 2 passed (no new src; drives Plan-4 + Plan-5 code).

- [ ] **Step 3: Commit**
```bash
git add bot/tests/test_chaos_drills.py
git commit -m "test: chaos drill - failed stop alerts and halts (deployment gate)"
```

---

### Task 4: Chaos drill — lost position on restart is caught by reconcile

**Files:** Modify `bot/tests/test_chaos_drills.py`

- [ ] **Step 1: Add the drill** (append):
```python
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
```

- [ ] **Step 2: Run, expect pass** — 4 passed in this file.

- [ ] **Step 3: Commit**
```bash
git add bot/tests/test_chaos_drills.py
git commit -m "test: chaos drill - lost/phantom position on restart halts"
```

---

### Task 5: Chaos drill — order timeout halts, but a fill that wins the race does NOT (no false halt) + full suite

**Files:** Modify `bot/tests/test_chaos_drills.py`

- [ ] **Step 1: Add the drill** (append) — drives the real `submit_and_verify`:
```python
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
```

- [ ] **Step 2: Run, expect pass** — 6 passed in this file.

- [ ] **Step 3: Run the FULL suite** — `python -m pytest` → expected 72 (Plans 1–4) + 17 (Plan 5) = 89 passed.

- [ ] **Step 4: Commit**
```bash
git add bot/tests/test_chaos_drills.py
git commit -m "test: chaos drill - order timeout halts; fill-wins-race no false halt"
```

---

## Self-review notes (done)
- **Spec coverage:** §7 truth ledger reconcile + drift > $50 halt + phantom/untracked guards → Task 1; §4 dead-man on failed-close flag → Task 2; §8 chaos drills (failed stop, lost/phantom position, order timeout) each alert+halt → Tasks 3–5.
- **No placeholders:** complete code + expected counts throughout.
- **Type consistency:** `DriftReport`/`reconcile`/`position_key` (Task 1) reused in Task 2 + Tasks 4–5; `Alert`/`Severity`/`alerts_for_cycle`/`should_halt_new_entries` (Task 2) reused in Tasks 3–5; drills consume Plan-4 `ExitResult.failed` and Plan-2 `submit_and_verify`/`OrderState` unchanged.
- **The deployment gate is real:** Task 3's failed-stop drill and Task 5's timeout drill both assert alert+halt; Task 5's fill-wins-race drill guards against false halts (so the gate is honest, not paranoid). Per spec §8, these passing is the precondition for live deployment.

## After Plan 5: the bot's safety + strategy core is complete and paper-ready. Remaining for go-live (separate effort, not a code plan): wire the live loop (schedule Monday 10:00 entry, poll cadence for the stop-monitor, live Tradier `http`), point at sandbox, run the deployment-gate drills against the real binary, then the paper→live gates in spec §8.
```
