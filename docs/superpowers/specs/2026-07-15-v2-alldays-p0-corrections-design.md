# v2 All-Days Bot — Priority-0 Corrections (Phase 1)

**Date:** 2026-07-15
**Branch:** `fix/alldays-v2-p0-corrections`
**Author:** Shawn Banks (advisor guidance relayed via `tradingadvice715.txt`)
**Scope:** Priority-0 items 1–7 only. Priority-1 (items 8–16) and the credit-qualification
redesign (interim two-lane rule + expectancy model) are **separate follow-on specs**.

## Context

An external advisor reviewed the v2 all-days bot (Bot B, sandbox) after the 7/15 session and
listed corrections. Items 1 and 5 were independently verified against the code before this spec
was written; all seven were read and confirmed accurate. This phase fixes the seven "fix
immediately" correctness/safety issues.

## Guardrails (apply to every change)

1. **Live real-money bot (Bot C / `run_s2b_live.py`) must not change trading behavior.** Bot C
   runs the shared codebase with all v2 flags OFF. Items 1, 3, 4, 5, 6, 7 are reachable only
   behind Bot-B flags (`transaction_cost_gate`, `credit_tiers`, `regime_shadow_monitor`,
   `aggregate_risk_budget`) and therefore cannot reach Bot C's path. **Item 2 (partial fills) is
   the sole exception** — it modifies the universal order path; it ships only with full test
   coverage and an explicit heads-up before merge.
2. **TDD.** Each change: add/adjust a failing test that pins the corrected behavior, then fix.
3. **Regression bar.** The full `bot/tests` suite must stay green; Bot C behavior verified
   unchanged (its tests + a targeted before/after behavior diff on a sandbox tick).
4. **No real-money deploy from this work.** Sandbox validation only, per the advisor.

## Item specs

### 1. Commission-calculation consistency
- **Current (verified):** `cost_gate.estimated_round_trip_cost` = `est_commission_per_leg_rt * 2`
  (= $1.30 at 0.65). `wiring._to_execution_result` synthesizes `est_commission_per_leg_rt * 2 *
  filled_quantity` **per order** — charged at both open and close → `0.65 * 4 = $2.60` round trip.
  The gate under-counts commission by 2×.
- **Change:** Rename the feature field to `commission_per_contract_per_leg_per_side = 0.65`. Add a
  single shared helper `round_trip_commission_per_contract(f) = commission_per_contract_per_leg_per_side * 2 (legs) * 2 (sides)`.
  `cost_gate` and the P&L/`wiring` synthetic-commission path both consume the same model
  (wiring charges half — one side — per order; the two orders sum to the round trip).
- **Files:** `bot/features.py`, `bot/strategy/cost_gate.py`, `bot/app/wiring.py`.
- **Tests:** `test_cost_gate.py` (round-trip cost now $8.60/contract at defaults); a wiring test
  asserting open+close synthetic commissions equal the gate's round-trip figure.
- **Bot C:** No behavioral change (live broker reports real fees; synthetic path unused live;
  cost gate is Bot-B-only). The rename is mechanical.

### 2. Partial-fill handling  *(touches live order path)*
- **Current (verified):** `orchestrator` records a position only on `status == "filled"` (entry
  path ~line 731). Partial opens leave broker contracts untracked (later reconciliation can halt);
  partial closes keep the full quantity managed and misbook realized P&L.
- **Change:** Branch on `execution_result.filled_quantity > 0`.
  - **Entry partial:** cancel the unfilled remainder; add `filled_quantity` to
    `state.open_positions` at the actual weighted fill credit; record fees on filled contracts only.
  - **Close partial:** compute realized P&L on the closed quantity; reduce the managed position's
    qty by the filled amount; keep managing the remainder; do **not** mark the position fully closed.
- **Files:** `bot/app/orchestrator.py`, `bot/broker/submit.py` (cancel-remainder helper if needed),
  `bot/app/state_store.py` (qty-reduction path).
- **Tests:** entry partial (2 of 5 fill → 2 tracked at weighted credit, remainder canceled);
  close partial (3 of 8 fill → P&L booked on 3, 5 still managed); a reconciliation test proving no
  spurious halt after a partial.
- **Bot C:** **Behavior changes** on partial fills (previously untracked → now tracked). This is
  the intended safety fix. Extra scrutiny; heads-up before merge.

### 3. Adaptive-credit-history deduplication
- **Current (verified):** `orchestrator` (~line 604) appends every evaluated candidate's ratio to
  `state.credit_ratio_history[bucket]` each cycle and truncates to the last 60. On a 240-cycle day
  this overwrites the 60-signal window with near-duplicate observations of the same spread.
- **Change:** Record a new observation only when, versus the last recorded observation for that
  DTE bucket, one holds: strike changed, expiry changed, credit ratio moved ≥ 0.5 pp, **or** a new
  15-minute research window began. Dedup key: `(trading_date, expiry, short_strike, long_strike,
  15-min time bucket)`. Apply the same dedup to rejected-signal markouts (see item 4).
- **Files:** `bot/app/orchestrator.py` (+ a small `credit_quality` helper for the record decision).
- **Tests:** 240 identical cycles → ≤1 history entry; a genuine strike/ratio change → new entry.
- **Bot C:** None (`credit_tiers` Bot-B-only).

### 4. Separate & redesign the markout collector
- **Current (verified):** markout tracking runs synchronously inside `run_entry_cycle`, gated on
  `regime_shadow_monitor`; each pending candidate is re-quoted (2 option requests) every tick. With
  200+ repeated candidates this can balloon to hundreds of quote calls per cycle on the critical
  order-management path.
- **Change:** Add a **separate** feature flag `markout_tracking` (default **False**); shadow
  monitor keeps its own flag and stays enabled. Redesign the tracker to: dedup signals (item-3
  key), batch all option symbols into as few `/markets/quotes` calls as possible, cache one mark per
  unique spread per cycle, query only when a horizon is actually due, run outside the
  order-management path, and report coverage/failure counts. Until validated, `markout_tracking`
  ships off.
- **Files:** `bot/features.py` (new flag), `bot/research/markouts.py`, `bot/app/orchestrator.py`,
  `bot/app/run_s2b_alldays.py` (leave `markout_tracking=False`).
- **Tests:** batching (N spreads → 1 quote call); a due-horizon-only update test; a "never raises
  into run_entry_cycle" test.
- **Bot C:** None (Bot-B-only; default off).

### 5. Replace the gap-stress model — Black-Scholes reprice + shock grid
- **Current (verified):** `gap_stress.stressed_spread_loss` uses intrinsic value only
  (`min(wing, max(0,short−S) − max(0,long−S))`), ignoring time value, IV expansion, skew, gamma,
  and bid/ask widening. For 743/733 at −1.5 ATR (S≈742.01) it yields ~$0.99 vs $1.39 credit →
  reports the position as still profitable under stress.
- **Change:** Reprice each leg with Black-Scholes across a scenario grid and take the **worst** cell.
  - **Spot grid:** SPY − {1.0, 1.5, 2.0} × ATR.
  - **IV grid:** base_iv + {3, 5, 10} vol points.
  - **Skew:** normal, and stressed (short leg gets an extra skew bump vs long, configurable, default
    +3 pts on the short under the stressed branch).
  - **Execution:** midpoint, and stressed-natural (short ask − long bid with a stress bid/ask
    widening factor, default 25%).
  - `base_iv` per leg from the Tradier greeks quote (`mid_iv`); fallback to a VIX-derived annualized
    IV when greeks are missing. Risk-free rate config default 0.04; T = DTE/365.
  - Worst-cell stressed spread value → loss = `(worst_debit − credit) * 100 * qty`. The risk gate
    (`gap_stress_ok`) uses this worst-cell loss.
- **Files:** `bot/portfolio/gap_stress.py` (new BS reprice + grid), a small BS pricer helper (new
  `bot/portfolio/bs.py`), `bot/app/wiring.py`/`orchestrator.py` (supply per-leg IV + rate config),
  `bot/features.py` (grid/rate/widening/skew-bump params + a `gap_stress_model` selector defaulting
  to the new model).
- **Tests:** BS pricer sanity (put price monotonic in spot/IV; matches a known value); grid returns
  a strictly larger loss than the intrinsic model for the 743/733 −1.5-ATR case; worst-cell
  selection; graceful fallback when IV missing.
- **Bot C:** None (`aggregate_risk_budget` Bot-B-only).

### 6. Foreign-position quantity matching
- **Current (verified):** `exposure.foreign_spy_exposure` excludes a broker spread entirely when its
  `(short, long, expiry)` key matches an own position — quantity-blind. If the broker holds 10 and
  this bot tracks 8, all 10 are excluded and the extra 2 foreign contracts are missed.
- **Change:** Match by key **and** compare quantities: `foreign_qty = max(0, broker_qty −
  own_tracked_qty)`; apply foreign risk to the difference. Also include foreign positions (at their
  foreign qty) in the 1.0/1.5/2.0-ATR gap-stress book, not just the stop/structural budgets.
- **Files:** `bot/portfolio/exposure.py`, and the gap-stress book assembly (orchestrator/wiring).
- **Tests:** broker 10 / own 8 → foreign risk on 2 contracts; foreign qty appears in gap-stress totals.
- **Bot C:** None (Bot-B-only).

### 7. Return the exact risk-budget reason
- **Current (verified):** `risk_budget.cap_to_budgets` returns an int; the decision log/report only
  records "risk-budget" as the reason (240-cycle day → "170 risk-budget blocks", no detail).
- **Change:** Add a `RiskBudgetResult` dataclass — `allowed_quantity`, `limiting_budget`
  (`same_day_stop | expiry_stop | total_stop | total_structural | None`), and the four current
  exposures + four limits. `cap_to_budgets` returns it. Provide an int-compatible accessor (or a
  thin wrapper) so no existing caller breaks. Orchestrator/report log the binding budget, current
  exposure, limit, headroom, proposed qty, permitted qty.
- **Files:** `bot/portfolio/risk_budget.py`, `bot/app/orchestrator.py`, `bot/ops/report.py`,
  `bot/app/wiring.py` (decision telemetry columns).
- **Tests:** a scenario where the expiry-stop budget binds → result names `expiry_stop` with correct
  headroom; backward-compat accessor returns the same int as before.
- **Bot C:** None (Bot-B-only; wrapper preserves any shared call site).

## Out of scope (this phase)
- Priority-1 items 8–16 (quote-age validation, ladder rung revalidation, TP natural-ceiling refresh,
  late-fill payload, execution metadata, settled-cash/strategy-NAV wiring, migration-lock state,
  shadow "unknown" vs "normal", live-v2 launcher decision).
- Credit-qualification redesign (interim two-lane rule + expectancy model + $5/$10 dynamic wing).
- Any real-money / live deploy.

## Verification / done criteria
- New + existing `bot/tests` all green.
- Bot C behavior verified unchanged (tests + before/after sandbox-tick behavior diff).
- A short before/after note per item (esp. item 5 numbers on the live 743/733 position).
