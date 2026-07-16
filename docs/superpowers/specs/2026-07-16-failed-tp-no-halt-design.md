# Design: Don't halt the bot on a failed take-profit close

**Date:** 2026-07-16
**Status:** Approved (brainstorm)
**Scope:** `bot/app/orchestrator.py` (`run_management_cycle`) + tests. Touches the shared live order-management path (Bot C, real money) — behavior change is intentional and safety-preserving.

## Problem

Bot C (the live $1-wing real-money bot, account 6YB71948) recurringly gets stuck in a
sticky `halt_reason="failed close"` that blocks all new entries until a human manually clears
it. Root cause: on a thin $1-wide spread the **take-profit** close (a buy-to-close for a few
cents) intermittently does not fill — Tradier cancels it, times it out, or returns a client
error. Today `run_management_cycle` treats *any* non-filled close as a CRITICAL alert and sets a
sticky halt (`bot/ops/monitor.py:22-27` → `should_halt_new_entries` → `orchestrator.py`
`run_management_cycle`), and that halt does **not** self-clear once the position later resolves
(the clear path only fires when a phantom position is reconciled away, so an empty book stays
halted forever).

A failed take-profit is **not a risk event** — the position is winning; we merely failed to
capture profit this tick. Halting new entries on it is both wrong and the direct cause of the
recurring outage. A failed **stop** (a losing position we could not exit) or a failed
**time-exit** (an expiry/assignment risk) *is* a must-exit risk case and should still halt.

## Decision (from brainstorm)

Classify a **fully-failed** management close (nothing filled) by its exit action:

| Failed close action | Behavior |
|---|---|
| `TAKE_PROFIT` | **WARN + retry next tick, NO halt** (new) |
| `STOP` | CRITICAL + halt (unchanged) |
| `TIME_EXIT` | CRITICAL + halt (unchanged — expiry/assignment risk) |
| `ERROR` (exception during close; original action lost) | CRITICAL + halt (unchanged — conservative) |

Management (`monitor_positions` → `decide_exit`) only ever produces `STOP`/`TAKE_PROFIT`/
`TIME_EXIT` (or `ERROR` on an exception). Discretionary closes (`DEGROSS`/`FLOW_DEGROSS`) run in
separate cycles that already WARN-and-retry without halting, so they are out of scope.

## The change

In `run_management_cycle`, the close-classification introduced for Task 2 already splits results
into `full_close` (removed), `partial_close` (booked + kept, non-halting WARN), and `hard_failed`
(nothing filled → currently `alerts_for_cycle` → CRITICAL → halt). Split `hard_failed` further by
action:

- `hard_failed` **AND** `r.action == TAKE_PROFIT` → append a non-halting WARN
  (`"take-profit close did not fill … will retry"`); do **not** feed it to `alerts_for_cycle`.
- `hard_failed` **AND** `r.action in {STOP, TIME_EXIT, ERROR}` → feed to `alerts_for_cycle`
  (CRITICAL) exactly as today, so `should_halt_new_entries` still halts.

`alerts_for_cycle` and `should_halt_new_entries` (`bot/ops/monitor.py`) are unchanged; we simply
control *which* failed closes reach them, mirroring the existing `partial_alerts` pattern. The
retry itself needs no new code — management re-runs every tick and re-attempts the close with
fresh quotes; the close is already priced at the marketable natural (`short.ask - long.bid`,
`wiring.py`), so successive attempts get repeated chances to fill.

## Non-goals (keep the real-money blast radius small)

- **Not** enabling the TP-ladder (`tp_price_ladder`) — a larger, still-unvalidated change to live
  close *pricing*. Retry-at-natural already gives repeated fill chances. Revisit separately only
  if TP fills still lag after this change.
- **Not** adding auto-clear for `STOP`/`TIME_EXIT` halts — a genuine failed stop *should* stick
  and get an operator.

## Bot C safety

This can only make the bot halt **less** on a non-risk event; it never removes a halt for a
genuine risk exit. Byte-identical for: successful closes (full or partial), STOP failures,
TIME_EXIT failures, ERROR failures, and every flags-off path unrelated to a failed take-profit.
The only behavior delta is: a fully-failed `TAKE_PROFIT` no longer halts (it WARNs + retries).

## Testing (TDD)

1. Failed `TAKE_PROFIT` (nothing filled) → `state.halted is False`, position retained, a WARN
   alert emitted, and the close is re-attempted on the next `run_management_cycle`.
2. Failed `STOP` → `state.halted is True`, `halt_reason` set (byte-identical to today).
3. Failed `TIME_EXIT` → halts (byte-identical).
4. Failed close that raised (`ExitAction.ERROR`) → halts (conservative).
5. Successful `TAKE_PROFIT` → position removed, no halt (byte-identical).
6. Partial `TAKE_PROFIT` (Task 2 path) → books on filled qty, keeps remainder, no halt
   (unchanged) — regression guard that the two classifications compose.
