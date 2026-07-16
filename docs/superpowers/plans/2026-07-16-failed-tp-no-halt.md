# Failed-Take-Profit No-Halt Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop Bot C's recurring sticky `"failed close"` halt by making a fully-failed **take-profit** close WARN-and-retry instead of halting, while a failed STOP / TIME_EXIT / ERROR still halts.

**Architecture:** One surgical change in `run_management_cycle` (`bot/app/orchestrator.py`), extending the Task-2 close-classification (`full_close` / `partial_close` / `hard_failed`). Split `hard_failed` by exit action: `TAKE_PROFIT` → non-halting WARN; `STOP`/`TIME_EXIT`/`ERROR` → CRITICAL (halt), exactly as today. `alerts_for_cycle` / `should_halt_new_entries` (`bot/ops/monitor.py`) are unchanged — we only control which failed closes reach them. Retry needs no new code (management re-runs every tick, close is already priced at marketable natural).

**Tech Stack:** Python 3.11, pytest, stdlib only.

**Test command:** `python -m pytest bot/tests -q` (single: `python -m pytest bot/tests/test_fill_accounting.py::test_x -v`). Windows / Git Bash.

**Spec:** `docs/superpowers/specs/2026-07-16-failed-tp-no-halt-design.md`

---

## File Structure

- Modify: `bot/app/orchestrator.py` — `run_management_cycle` only (the `hard_failed` handling + the final `alerts` composition).
- Test: `bot/tests/test_fill_accounting.py` — new failed-TP / failed-STOP / failed-TIME_EXIT / failed-ERROR / successful-TP tests (this file already hosts the Task-2 close-classification tests and their fixtures `_er`, `_deps`, `_chain`, `MONDAY`, `ManagedPosition`, `BotState`).

---

## Task 1: Failed take-profit warns + retries instead of halting

**Files:**
- Modify: `bot/app/orchestrator.py` (`run_management_cycle`)
- Test: `bot/tests/test_fill_accounting.py`

### Context the implementer needs (read before coding)

`run_management_cycle` (`bot/app/orchestrator.py`) currently builds three buckets from `monitor_positions` results:
- `full_close` (`not r.failed` or `cfq >= r.position.qty`) → position removed.
- `partial_close` (`r.failed` and `0 < cfq < r.position.qty`) → books P&L on `cfq`, keeps remainder, appends a **non-halting** `Alert(Severity.WARN, ...)` to `partial_alerts`.
- `hard_failed` (`r.failed` and `cfq == 0`) → collected into a `hard_failed` list.

At the end it composes: `alerts = alerts_for_cycle(hard_failed, drift_report=None) + partial_alerts`, then `should_halt_new_entries(alerts)` halts on any CRITICAL. `alerts_for_cycle` (`bot/ops/monitor.py:18`) emits a CRITICAL for every result it's given. So today **every** `hard_failed` close halts.

`r.action` is an `ExitAction` (`bot/strategy/manage.py`): management produces `STOP`, `TAKE_PROFIT`, `TIME_EXIT`, or `ERROR` (the last set by `monitor_positions` when the close raises). `ExitAction` is imported in `orchestrator.py` already (used elsewhere). `Alert`/`Severity` are imported too (used by `partial_alerts`).

**The change:** split `hard_failed` by action. Only `STOP`/`TIME_EXIT`/`ERROR` failures go to `alerts_for_cycle` (halt). A failed `TAKE_PROFIT` gets a non-halting WARN instead. Retry is automatic (next tick re-runs management).

- [ ] **Step 1: Write the failing tests** — add to `bot/tests/test_fill_accounting.py` (reuse existing `_er`, `_deps`, `ManagedPosition`, `BotState`; a mark of `3.5` with `credit=1.00` and default `ManageConfig` `stop_mult=2.0` triggers a STOP; a mark `<= credit*(1-0.5)` triggers TAKE_PROFIT; `dte_of <= 1` triggers TIME_EXIT):

```python
def test_failed_take_profit_warns_and_does_not_halt():
    # winning position: mark 0.40 <= credit*(1-tp_pct)=0.50 -> TAKE_PROFIT; close fills NOTHING.
    pos = ManagedPosition("SPY", 568.0, 558.0, credit=1.00, qty=4, expiry="2026-06-19")
    state = BotState(open_positions=[pos])
    d = _deps(mark_position=lambda p: 0.40, dte_of=lambda p, today: 5,
              close_spread=lambda p, a: _er(status="canceled", requested_qty=4, filled_qty=0))
    state, results = run_management_cycle(state, d, today="2026-06-19")
    assert results[0].action == ExitAction.TAKE_PROFIT and results[0].failed is True
    assert state.halted is False                      # a failed take-profit must NOT halt
    assert state.open_positions == [pos]              # position kept for retry next tick

def test_failed_take_profit_is_retried_next_cycle():
    # the same TAKE_PROFIT is re-attempted on the next management cycle (close_fn called again).
    calls = {"n": 0}
    def close_fn(p, a):
        calls["n"] += 1
        return _er(status="canceled", requested_qty=4, filled_qty=0)
    pos = ManagedPosition("SPY", 568.0, 558.0, credit=1.00, qty=4, expiry="2026-06-19")
    state = BotState(open_positions=[pos])
    d = _deps(mark_position=lambda p: 0.40, dte_of=lambda p, today: 5, close_spread=close_fn)
    state, _ = run_management_cycle(state, d, today="2026-06-19")
    state, _ = run_management_cycle(state, d, today="2026-06-19")
    assert calls["n"] == 2                             # re-attempted, not abandoned
    assert state.halted is False

def test_failed_stop_still_halts_bot_c():
    # losing position: mark 3.5 >= credit*(1+stop_mult)=3.00 -> STOP; close fills nothing -> HALT.
    pos = ManagedPosition("SPY", 568.0, 558.0, credit=1.00, qty=4, expiry="2026-06-19")
    state = BotState(open_positions=[pos])
    d = _deps(mark_position=lambda p: 3.5, dte_of=lambda p, today: 5,
              close_spread=lambda p, a: _er(status="timeout", requested_qty=4, filled_qty=0))
    state, results = run_management_cycle(state, d, today="2026-06-19")
    assert results[0].action == ExitAction.STOP
    assert state.halted is True and "close" in state.halt_reason

def test_failed_time_exit_still_halts():
    # dte 0 (<= time_exit_dte 1) with a non-stop, non-tp mark -> TIME_EXIT; close fails -> HALT.
    pos = ManagedPosition("SPY", 568.0, 558.0, credit=1.00, qty=4, expiry="2026-06-19")
    state = BotState(open_positions=[pos])
    d = _deps(mark_position=lambda p: 1.00, dte_of=lambda p, today: 0,
              close_spread=lambda p, a: _er(status="canceled", requested_qty=4, filled_qty=0))
    state, results = run_management_cycle(state, d, today="2026-06-19")
    assert results[0].action == ExitAction.TIME_EXIT
    assert state.halted is True

def test_failed_close_that_raised_still_halts():
    # close_fn raises -> monitor_positions records ExitAction.ERROR, failed=True -> HALT (conservative).
    def boom(p, a):
        raise RuntimeError("broker 500")
    pos = ManagedPosition("SPY", 568.0, 558.0, credit=1.00, qty=4, expiry="2026-06-19")
    state = BotState(open_positions=[pos])
    d = _deps(mark_position=lambda p: 0.40, dte_of=lambda p, today: 5, close_spread=boom)
    state, results = run_management_cycle(state, d, today="2026-06-19")
    assert results[0].action == ExitAction.ERROR
    assert state.halted is True

def test_successful_take_profit_still_removes_position_no_halt():
    # byte-identical happy path: TAKE_PROFIT fills fully -> position removed, no halt.
    pos = ManagedPosition("SPY", 568.0, 558.0, credit=1.00, qty=4, expiry="2026-06-19")
    state = BotState(open_positions=[pos])
    d = _deps(mark_position=lambda p: 0.40, dte_of=lambda p, today: 5,
              close_spread=lambda p, a: _er(status="filled", requested_qty=4, filled_qty=4))
    state, results = run_management_cycle(state, d, today="2026-06-19")
    assert state.open_positions == []
    assert state.halted is False
```

- [ ] **Step 2: Run the tests, expect failures** — `python -m pytest bot/tests/test_fill_accounting.py -k "take_profit or failed_stop or failed_time_exit or failed_close_that_raised" -v`. Expected: `test_failed_take_profit_warns_and_does_not_halt`, `test_failed_take_profit_is_retried_next_cycle` FAIL (today a hard-failed TAKE_PROFIT halts → `state.halted` is True, so the `assert state.halted is False` fails); the STOP / TIME_EXIT / ERROR / successful-TP tests may already PASS (they assert today's behavior) — that's fine, they are regression guards.

- [ ] **Step 3: Implement** — in `bot/app/orchestrator.py` `run_management_cycle`, change the `hard_failed` handling so a failed `TAKE_PROFIT` becomes a non-halting WARN and only the risk actions reach `alerts_for_cycle`. Concretely:

  Where the loop currently appends a `hard_failed` result:
  ```python
          else:
              hard_failed.append(r)     # filled nothing -> a genuine stuck close (alert + halt)
  ```
  replace with (routing a failed TAKE_PROFIT to a non-halting WARN instead of the halting set):
  ```python
          elif r.action == ExitAction.TAKE_PROFIT:
              # A failed TAKE-PROFIT is NOT a risk event (the position is winning; we just didn't
              # capture profit this tick). It must NOT set the sticky "failed close" halt -- that
              # was the root cause of Bot C's recurring $1-wing halt. WARN and let the next
              # management cycle retry the close (re-priced at fresh natural). Only STOP/TIME_EXIT/
              # ERROR (real must-exit risk) still halt (fall through to hard_failed below).
              partial_alerts.append(Alert(Severity.WARN,
                  f"take-profit close did not fill ({r.close_status}) for {r.position.ticker} "
                  f"{r.position.short_strike}/{r.position.long_strike}: qty {r.position.qty} kept, will retry"))
          else:
              hard_failed.append(r)     # STOP / TIME_EXIT / ERROR that filled nothing -> alert + halt
  ```
  (`partial_alerts` is already the non-halting WARN bucket composed into `alerts` alongside `alerts_for_cycle(hard_failed, ...)`; reusing it means the failed-TP position is neither removed — it's not in `closed_ok` — nor halted. No other change is needed. Do NOT alter `alerts_for_cycle`/`should_halt_new_entries`.)

- [ ] **Step 4: Run the tests, expect pass** — `python -m pytest bot/tests/test_fill_accounting.py -k "take_profit or failed_stop or failed_time_exit or failed_close_that_raised" -v`. Expected: all PASS.

- [ ] **Step 5: Run the full suite** — `python -m pytest bot/tests -q`. Expected: all green (no regression; the Task-2 `test_close_full_failed_zero_fill_still_halts_bot_c` uses a STOP-triggering mark 3.5, so it still halts and stays green).

- [ ] **Step 6: Commit** —
```bash
git add bot/app/orchestrator.py bot/tests/test_fill_accounting.py
git commit -m "Fix recurring halt: failed take-profit warns + retries instead of halting"
```

---

## Self-Review

- **Spec coverage:** the decision table (TAKE_PROFIT → WARN+retry; STOP/TIME_EXIT/ERROR → halt) maps to Step 3 + tests 1–5; the "retry needs no new code" claim is verified by `test_failed_take_profit_is_retried_next_cycle`; the partial-TP compose guard from the spec is already covered by the existing Task-2 `test_close_partial_...` tests (the change only touches the `cfq == 0` branch, leaving `partial_close` untouched). ✅
- **Placeholder scan:** every step has concrete test/impl code and exact commands. ✅
- **Type consistency:** `ExitAction.TAKE_PROFIT/STOP/TIME_EXIT/ERROR`, `Alert`, `Severity.WARN`, `partial_alerts`, `hard_failed`, `run_management_cycle` used consistently with the existing code. ✅
