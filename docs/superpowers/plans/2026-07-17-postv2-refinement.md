# Post-v2 Refinement — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. Every task is TDD against `bot/tests`.

**Goal:** Implement the user's "Post-v2 Refinement" spec so the bot no longer treats *"the requested size does not fit"* as *"no version of this trade can fit"* — converting every risk gate into a **quantity cap**, fixing probe-rounding-to-zero, sizing gap-stress incrementally, adding richer telemetry, candidate/credit dedup, alternate-expiration evaluation, and a shadow-only $5-wing fallback.

**Spec (verbatim):** `docs/superpowers/specs/2026-07-17-postv2-refinement-spec.md` — read the exact function bodies/thresholds there; this plan maps each numbered section to the codebase.

**Rollout (from the spec + user):** Deploy the *active* changes to **Bot B (sandbox) AND Bot C (live) together**. Keep the **$5-wing fallback, wider-risk comparison, bearish module** shadow-only. **Do NOT loosen** the credit floors, structural limit, hard stop, or trend gate. Fidelity: implement the spec's exact logic/thresholds **integrated into the existing modules** (reuse `risk_budget.py`/`credit_quality.py`/`gap_stress.py` functions that already match; refactor the rest — no duplicate parallel logic).

**Tech stack:** Python 3.11, pytest, stdlib. **Test command:** `python -m pytest bot/tests -q`.

**Reconciliation (what already exists — reuse, don't duplicate):**
- `bot/portfolio/risk_budget.py` already has `planned_stop_loss_per_contract` (§4), `remaining_stop_risk` (§5), `structural_max_loss_per_contract` (§7), and `cap_to_budgets` returning a `RiskBudgetResult` with a `limiting_budget` (§3/§10/§11 in embryo). **Refactor** these toward the spec's `QuantityCap`/`RiskSizingResult` rather than re-adding.
- `bot/strategy/credit_quality.py` has `credit_tier` (§1 in embryo) + `should_record_observation` (§13). **Extend** for the low-credit-safety lane and align dedup.
- `bot/portfolio/gap_stress.py` has `stressed_spread_loss_bs` (§9 BS repricing) + `gap_stress_losses`. Gap-stress is currently a **shrink-loop** in `orchestrator.run_entry_cycle` — **convert** to incremental `QuantityCap`s (§8).
- The entry-sizing pipeline lives in `bot/app/orchestrator.py` `run_entry_cycle` (the `aggregate_risk_budget` block). That is where the new pipeline is wired.

---

## File Structure

- `bot/portfolio/risk_budget.py` — `QuantityCap`, `quantity_cap_from_budget`, `RiskSizingResult`, `size_to_risk_limits`, `apply_quality_multiplier`; refactor `cap_to_budgets` to emit `QuantityCap`s (keep an int-compatible shim for back-compat). (§2,§3,§4,§5,§6,§7,§10,§11)
- `bot/portfolio/gap_stress.py` — `gap_quantity_cap`, per-scenario `StressScenario` matrix (§8,§9).
- `bot/strategy/credit_quality.py` — `classify_credit_quality` with the low-credit-safety lane + `low_credit_safety_pass` conditions; `candidate_key`, `CreditObservation` (§1,§12,§13).
- `bot/app/orchestrator.py` — wire the new pipeline into `run_entry_cycle`; alternate-expiration loop; `EntryState`; report counters (§14,§16,§17). Emit the §11 telemetry.
- `bot/strategy/fallback.py` — **new** — $5-wing shadow evaluation (§15).
- `bot/ops/report.py` — the expanded daily-report fields (§17).
- `bot/features.py` — new flags/thresholds (`ENABLE_FIVE_DOLLAR_WING_SHADOW/LIVE`, alt-expiration count, stress-scenario IV shocks) — Bot-C-safe defaults.
- Tests under `bot/tests/` — the §18 tests plus per-task TDD.

---

## PHASE A — active changes (deploy to Bot B + Bot C)

### Task 1: Probe-rounding fix — `apply_quality_multiplier` (§2)
**Files:** `bot/portfolio/risk_budget.py`; test `bot/tests/test_risk_budget.py`.
- [ ] **Step 1 (failing test):** add the spec's `test_probe_size_never_rounds_valid_base_qty_to_zero` — `apply_quality_multiplier(base_qty=2, multiplier=0.40) == 1`.
- [ ] **Step 2:** run → FAIL (function missing).
- [ ] **Step 3:** add `apply_quality_multiplier(base_qty, multiplier, maximum_qty=None)` exactly per spec §2 (probe multiplier `<1.0` → `max(1, floor(base*mult))`; `>=1.0` → `floor`; then clamp to `maximum_qty`). Do **not** re-introduce a global min-1 *after* risk checks — this only guarantees a *candidate*.
- [ ] **Step 4:** in `size_qty`, split the quality-multiplier step out so probe no longer floors to 0 pre-cap; base qty is `min(qty_entry, qty_structural)` and the quality multiplier is applied via `apply_quality_multiplier` (returns the candidate qty that then flows into the caps). Keep the "NO forced one-contract minimum *after* risk" contract.
- [ ] **Step 5:** full suite green. **Commit.**

### Task 2: Credit lanes + low-credit-safety (§1)
**Files:** `bot/strategy/credit_quality.py`; `bot/features.py` (thresholds); test `test_credit_quality.py`.
- [ ] Add module constants `ABSOLUTE_CREDIT_FLOOR=0.08`, `STANDARD_PROBE_FLOOR=0.10`, `FULL_SIZE_CREDIT_FLOOR=0.115`, `LOW_CREDIT_MAX_QTY=1`, `PROBE_SIZE_MULTIPLIER=0.40`.
- [ ] Add `classify_credit_quality(credit_ratio, adaptive_full_size_threshold) -> (tier, size_multiplier, maximum_qty)` exactly per spec §1 (reject / low_credit_safety 0.25×,max 1 / probe 0.40× / full). Keep the existing `credit_tier` as a thin back-compat wrapper OR migrate callers.
- [ ] Add `low_credit_safety_pass(...)` evaluating the spec's exact conjunction (`target_to_cost_ratio>=4.5 and package_width_ratio<=0.20 and quote_age_seconds<=2.0 and cushion_atr>=1.15 and (expected_move_cushion>=0.90 or short_delta<=0.30) and not defensive_market_state`).
- [ ] Wire into `run_entry_cycle`: a low-credit candidate is only allowed if `low_credit_safety_pass` is True; it must still pass every hard risk cap (do not bypass).
- [ ] **TDD:** low-credit lane allowed only with all conditions; rejected if any fails; probe/full unchanged. Full suite green. **Commit.**

### Task 3: QuantityCap + incremental gap-stress caps (§3, §8, §9)
**Files:** `bot/portfolio/risk_budget.py`, `bot/portfolio/gap_stress.py`; tests.
- [ ] Add `QuantityCap` dataclass (§3) and `quantity_cap_from_budget(name, current_exposure, limit, incremental_risk_per_contract)` (§4).
- [ ] Refactor the four stop/structural budgets in `cap_to_budgets` to build `QuantityCap`s via `quantity_cap_from_budget` (reuse existing `remaining_stop_risk`/`structural_max_loss_per_contract`). Preserve the existing exposure/limit math.
- [ ] Add `gap_quantity_cap(scenario_name, current_book_loss, one_contract_book_loss, loss_limit)` (§8) and a `StressScenario` matrix `[down_1atr(-1.0,+3iv), down_1_5atr(-1.5,+5iv), down_2atr(-2.0,+10iv)]` with `STRESSED_CLOSE_SLIPPAGE=0.10` (§9) wired into `stressed_spread_loss_bs` (align IV shock per scenario). Compute current-book vs current-book+1-candidate stressed loss → incremental per contract.
- [ ] **TDD:** the spec's `test_gap_gate_uses_incremental_candidate_loss` (incremental 250 → maximum_qty 2). Gap caps per scenario; strictest binds. Full suite green. **Commit.**

### Task 4: RiskSizingResult + telemetry + decision outcomes (§10, §11)
**Files:** `bot/portfolio/risk_budget.py`, `bot/app/orchestrator.py`, `bot/app/wiring.py` (decision-log fields); tests.
- [ ] Add `RiskSizingResult` (§10) + `size_to_risk_limits(requested_qty, caps)` — `final_qty=min(requested, min-cap)`, `limiting_gate`, `allowed`. Keep `RiskBudgetResult` int-compat shim so existing callers don't break, OR migrate them.
- [ ] Emit §11 telemetry in the DECISION log: `limiting_gate`, `requested_qty`, `quality_adjusted_qty`, `final_qty`, and the binding cap's `current_exposure`/`limit`/`remaining_capacity`/`incremental_risk_per_contract`. Add the fields to `_DECISION_LOG_FIELDS` in `wiring.py` (guard against the silent-drop bug class).
- [ ] Distinct decision outcomes: `allowed_full` / `allowed_reduced` / `blocked_zero_capacity` / `blocked_credit_quality` / `blocked_transaction_cost` / `blocked_quote_quality` / `blocked_duplicate` / `blocked_regime`.
- [ ] **TDD:** spec's `test_probe_minimum_does_not_override_zero_risk_capacity`, `test_oversized_order_is_reduced_not_rejected`. Full suite green. **Commit.**

### Task 5: Wire the quantity-cap pipeline into `run_entry_cycle` (§3 integration)
**Files:** `bot/app/orchestrator.py`; tests.
- [ ] Replace the current `size_qty` → `cap_to_budgets` → **gap-stress shrink-loop** with: base qty → `apply_quality_multiplier` (quality) → build ALL `QuantityCap`s (4 budgets + 3 gap scenarios) → `size_to_risk_limits` → `final_qty`. Reduce-not-reject: a nonzero `final_qty` trades at the reduced size; only `final_qty<=0` rejects. Preserve the existing OPEN-row + gap-stress logging (log the *enforced* gap numbers).
- [ ] Bot-C safety: the pipeline is inside the `aggregate_risk_budget` block (both bots have it on). Confirm the shared full-fill/zero-fill/partial paths from prior tasks stay intact.
- [ ] **TDD:** an oversized request is reduced (not rejected); a genuinely-full book still rejects (final_qty 0). Full suite green. **Commit.**

### Task 6: Candidate-signal dedup for reporting (§12)
**Files:** `bot/strategy/credit_quality.py` (or new `bot/strategy/dedup.py`), `bot/app/orchestrator.py`, `BotState`; tests.
- [ ] Add `candidate_key(trading_date, expiry, short, long, timestamp)` (15-min bucket) per spec. Maintain a persisted `seen_candidate_keys` set. Track raw-polling vs unique-opportunity counts. A candidate is "new" on strike/expiry change, new 15-min bucket, or credit-ratio move ≥ 0.005.
- [ ] **TDD:** spec's `test_same_signal_same_bucket_is_not_counted_twice`. Full suite green. **Commit.**

### Task 7: Adaptive credit-history dedup alignment (§13)
**Files:** `bot/strategy/credit_quality.py`; tests.
- [ ] Confirm `should_record_observation` matches §13 (`new_candidate_key or |Δratio|>=0.005`); add `CreditObservation` dataclass (timestamp/expiry/dte_bucket/strikes/exec_credit/ratio/candidate_key) if the history needs the richer record. Only prior observations feed the current threshold (already true).
- [ ] **TDD:** dedup by key + 0.005 move; no look-ahead. Full suite green. **Commit.**

### Task 8: Alternate-expiration evaluation (§14)
**Files:** `bot/app/orchestrator.py`, new `get_next_eligible_expirations`; `CandidateEvaluation`; tests.
- [ ] Evaluate up to 3 eligible expirations (min_dte 4): build best spread, credit quality, target-to-cost, all risk caps, final qty, expected target profit → `CandidateEvaluation`. Select `max(final_qty>0, key=(expected_target_profit, credit_ratio, -dte))`; `no_candidate_fits` if none. A later expiry must still pass credit/cost/trend/regime rules (no bypass).
- [ ] **TDD:** spec's `test_later_expiration_selected_when_nearest_has_zero_capacity`. Full suite green. **Commit.**

### Task 9: Operational states (§16)
**Files:** `bot/app/orchestrator.py`; tests.
- [ ] `EntryState` enum (ACTIVE/NO_QUALIFIED_CREDIT/NO_RISK_CAPACITY/NO_GAP_CAPACITY/NO_VALID_QUOTES/NO_VALID_EXPIRATION/RISK_OFF/DATA_FAILURE). Report state once per unique candidate or state transition — NOT every polling cycle.
- [ ] **TDD:** state transitions logged once, not per tick. Full suite green. **Commit.**

### Task 10: Daily-report changes (§17)
**Files:** `bot/ops/report.py`; tests.
- [ ] Add the §17 fields: raw polling cycles, unique candidate opportunities, unique strike pairs, unique expirations; per-gate rejection counts; allowed-full/reduced/probe/blocked; hypothetical fallback (later-expiry / $5-wing / balanced-risk) availability. **Do not** use raw polling in profitability reports.
- [ ] **TDD:** report renders the new sections from a synthetic decision log. Full suite green. **Commit.**

---

## PHASE B — shadow-only (log, never trade)

### Task 11: $5-wing fallback in shadow (§15)
**Files:** new `bot/strategy/fallback.py`, `bot/app/orchestrator.py`, `bot/features.py`; tests.
- [ ] For every otherwise-valid $10 candidate reduced to zero by risk limits, build a $5-wide alt at the same short strike; evaluate credit ratio, target-to-cost, expected profit, structural max loss, planned stop, incremental gap risk, bid/ask width, final permissible qty. **Log only** (would it have qualified? what qty? markout). Flags `ENABLE_FIVE_DOLLAR_WING_SHADOW=True`, `ENABLE_FIVE_DOLLAR_WING_LIVE=False` (both bots — shadow only).
- [ ] **TDD:** shadow evaluation runs + logs, never emits an order. Full suite green. **Commit.**

---

## Final steps
- [ ] Full `python -m pytest bot/tests -q` green; whole-branch review.
- [ ] Confirm the **credit floors / structural 15% / hard stop / trend gate are UNCHANGED** (spec: do not loosen).
- [ ] Deploy shared code to `/root/s2b-bot`; restart **Bot B and Bot C**; verify both stable, book==broker, and that reduced-qty entries now appear where full-qty was previously rejected.

## Self-Review
- **Spec coverage:** §1→T2, §2→T1, §3/§4/§6/§7→T3, §5→T3(reuse), §8/§9→T3, §10/§11→T4, wiring→T5, §12→T6, §13→T7, §14→T8, §16→T9, §17→T10, §15→T11, §18 tests woven per task. ✅
- **Don't-loosen guard:** thresholds kept; low-credit-safety lane is *additive with strict conditions*, not a floor drop. ✅
- **Bot-C safety:** everything rides the existing `aggregate_risk_budget` path; the behavior change (reduce-not-reject) is the intended one; $5-wing/wider-risk/bearish stay shadow. ✅
