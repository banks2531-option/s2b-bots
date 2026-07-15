# v2 All-Days P0 Corrections — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the seven Priority-0 correctness/safety defects in the v2 all-days bot (advisor review, `tradingadvice715.txt`).

**Architecture:** Small, focused edits behind existing Bot-B feature flags (except partial-fill handling, which is on the universal order path). TDD against the existing `bot/tests` suite. Bot C (live, real money) behavior must stay unchanged except the partial-fill fix.

**Tech Stack:** Python 3.11, pytest, stdlib only (Black-Scholes via `math`).

**Test command:** `python -m pytest bot/tests -q` (single test: `python -m pytest bot/tests/test_x.py::test_y -v`). Windows / Git Bash.

**Suggested sequence:** 1 → 7 → 6 → 5 → 3 → 4 → 2 (mechanical first; partial-fill last so it's reviewed in isolation).

---

## File Structure

- `bot/features.py` — rename commission field; add gap-stress grid params, `markout_tracking` flag, `gap_stress_model` selector, `risk_free_rate`.
- `bot/strategy/cost_gate.py` — shared round-trip commission helper (item 1).
- `bot/app/wiring.py` — synthetic commission via shared helper; supply per-leg IV to gap-stress (items 1, 5).
- `bot/portfolio/bs.py` — **new** Black-Scholes put pricer (item 5).
- `bot/portfolio/gap_stress.py` — BS reprice + shock grid, worst-cell loss (item 5).
- `bot/portfolio/exposure.py` — quantity-aware foreign matching (item 6).
- `bot/portfolio/risk_budget.py` — `RiskBudgetResult` dataclass + int-compat accessor (item 7).
- `bot/app/orchestrator.py` — partial-fill handling (item 2), credit-history dedup (item 3), markout dedup + batching + flag (item 4), risk-budget reason logging (item 7), foreign qty into gap-stress book (item 6).
- `bot/research/markouts.py` — batched resolve; coverage reporting (item 4).
- `bot/app/run_s2b_alldays.py` — leave `markout_tracking=False` (item 4).
- Tests: matching files under `bot/tests/`.

---

## Task 1: Commission-calculation consistency (item 1)

**Files:**
- Modify: `bot/features.py` (rename `est_commission_per_leg_rt` → `commission_per_contract_per_leg_per_side`)
- Modify: `bot/strategy/cost_gate.py:4-9`
- Modify: `bot/app/wiring.py:197`
- Test: `bot/tests/test_cost_gate.py`

- [ ] **Step 1: Failing test** — add to `bot/tests/test_cost_gate.py`:
```python
def test_round_trip_commission_is_two_legs_two_sides():
    from bot.strategy.cost_gate import round_trip_commission_per_contract
    from bot.features import S2bFeatures
    f = S2bFeatures()  # 0.65 default
    assert round_trip_commission_per_contract(f) == 0.65 * 2 * 2  # 2 legs x 2 sides = 2.60

def test_round_trip_cost_uses_full_commission():
    from bot.strategy.cost_gate import estimated_round_trip_cost
    from bot.features import S2bFeatures
    f = S2bFeatures()
    # commission 2.60 + slippage (0.03+0.03)*100 = 6.00  -> 8.60
    assert estimated_round_trip_cost(f) == 8.60
```
- [ ] **Step 2: Run, expect fail** — `python -m pytest bot/tests/test_cost_gate.py -q` → FAIL (no `round_trip_commission_per_contract`; old cost = 7.30).
- [ ] **Step 3: Rename field** in `bot/features.py`: change `est_commission_per_leg_rt: float = 0.65` to `commission_per_contract_per_leg_per_side: float = 0.65`.
- [ ] **Step 4: Implement** in `bot/strategy/cost_gate.py`:
```python
def round_trip_commission_per_contract(f):
    """2 legs x 2 sides (entry + exit), per contract."""
    return f.commission_per_contract_per_leg_per_side * 2 * 2

def estimated_round_trip_cost(f):
    commission = round_trip_commission_per_contract(f)
    slippage = (f.expected_entry_slippage + f.expected_entry_slippage) * 100.0
    return commission + slippage
```
- [ ] **Step 5: Fix wiring** `bot/app/wiring.py:197` — the synthetic per-order commission is *one side* (2 legs). Replace `features.est_commission_per_leg_rt * 2 * filled_quantity` with `features.commission_per_contract_per_leg_per_side * 2 * filled_quantity` and add a comment: `# one side (2 legs); the paired open+close order sums to round_trip_commission_per_contract`.
- [ ] **Step 6: Grep for other references** — `python -m pytest`-independent: run Grep for `est_commission_per_leg_rt` across `bot/`; update every hit (tests included).
- [ ] **Step 7: Run full suite** — `python -m pytest bot/tests -q` → PASS.
- [ ] **Step 8: Commit** — `git add -A && git commit -m "Fix item 1: round-trip commission consistency (cost gate == synthetic accounting)"`

---

## Task 2: Partial-fill handling (item 2) — *touches live order path*

**Files:**
- Modify: `bot/app/orchestrator.py` (entry: 729-769; close: within `run_management_cycle`)
- Modify: `bot/broker/submit.py` (add `cancel_order`/remainder-cancel helper if not present)
- Test: `bot/tests/test_orchestrator.py`, `bot/tests/test_fill_accounting.py`

### 2a — Entry partial fill

- [ ] **Step 1: Failing test** in `bot/tests/test_fill_accounting.py`:
```python
def test_entry_partial_fill_records_filled_qty_and_cancels_remainder(make_deps):
    # open_spread returns ExecutionResult(status="partially_filled", requested_quantity=5,
    #   filled_quantity=2, average_fill_price=1.10, commissions=..., order_id="O1")
    # Expect: 2 contracts tracked at credit 1.10; remainder (3) cancel requested; no halt.
    state, deps = make_deps(open_result=partial(filled=2, requested=5, price=1.10, oid="O1"))
    state, status = run_entry_cycle(state, deps, NOW, regime=None)
    pos = state.open_positions[-1]
    assert pos.qty == 2 and pos.credit == 1.10
    assert deps.canceled_orders == ["O1"]   # remainder canceled
```
(Add a `partial()` ExecutionResult factory + a fake `open_spread`/`cancel_order` to the test's `make_deps`.)
- [ ] **Step 2: Run, expect fail** — `python -m pytest bot/tests/test_fill_accounting.py -k entry_partial -v` → FAIL (partial ignored today; `status == "filled"` is false so nothing recorded).
- [ ] **Step 3: Implement** — in `orchestrator.py` change the gate at line 731 from `if status == "filled":` to handle partials. New logic:
```python
filled_qty = getattr(open_result, "filled_quantity", 0) or 0
if status in ("filled", "partially_filled") and filled_qty > 0:
    if status == "partially_filled":
        oid = getattr(open_result, "order_id", None)
        if oid is not None:
            deps.cancel_order(oid)          # cancel the unfilled remainder
    credit, qty, opening_fees = order.credit, filled_qty, 0.0
    if deps.features.actual_fill_accounting:
        fp = getattr(open_result, "average_fill_price", None)
        if fp is not None: credit = fp
        opening_fees = (getattr(open_result,"commissions",0.0) or 0.0) + \
                       (getattr(open_result,"regulatory_fees",0.0) or 0.0)
    # ... existing _log_decision + append ManagedPosition(..., qty, ...) using filled qty ...
else:
    _log_decision(status, spot=spot, atr=atr, expiry=expiry, order=order)
```
Add `cancel_order` to `Deps` and wire it in `wiring.py` to `broker.submit.cancel_order`. In `broker/submit.py` add:
```python
def cancel_order(client, account_id, order_id):
    return client.request("DELETE", f"/accounts/{account_id}/orders/{order_id}")
```
- [ ] **Step 4: Run, expect pass** — same command → PASS.
- [ ] **Step 5: Commit** — `git commit -am "Fix item 2a: record entry partial fills, cancel remainder"`

### 2b — Close partial fill

- [ ] **Step 6: Failing test** in `bot/tests/test_fill_accounting.py`:
```python
def test_close_partial_books_pnl_on_closed_qty_and_keeps_remainder(make_deps):
    # position qty 8 credit 1.00; close_spread returns partially_filled filled_quantity=3 at debit 0.40
    # Expect: realized P&L booked on 3 contracts ((1.00-0.40)*100*3=180), position now qty 5, still managed.
    ...
    assert booked_pnl == 180.0
    assert pos.qty == 5
    assert pos in state.open_positions        # remainder still managed, NOT marked closed
```
- [ ] **Step 7: Run, expect fail** — → FAIL (today the close path only acts on full `filled`, misbooks/over-manages).
- [ ] **Step 8: Implement** — in `run_management_cycle`'s close handling, when the close returns `partially_filled` with `filled_quantity>0`: compute `pnl=(entry_credit-close_debit)*100*filled_qty`, append a CLOSE row for `filled_qty`, set `p.qty -= filled_qty`, and keep `p` in `open_positions` (only remove when `p.qty==0`). Do NOT mark full close.
- [ ] **Step 9: Run + full suite** — `python -m pytest bot/tests -q` → PASS.
- [ ] **Step 10: Bot C guard** — add/confirm a test that with all flags off and a normal full fill, entry+close behavior is byte-identical (position qty, credit, P&L, log rows) to pre-change. Run it.
- [ ] **Step 11: Commit** — `git commit -am "Fix item 2b: close partial fills book P&L on closed qty, manage remainder"`

---

## Task 3: Adaptive-credit-history deduplication (item 3)

**Files:** Modify `bot/app/orchestrator.py:592-606`; add helper in `bot/strategy/credit_quality.py`. Test: `bot/tests/test_credit_quality.py`.

- [ ] **Step 1: Failing test**:
```python
def test_credit_history_dedups_repeated_identical_candidates():
    from bot.strategy.credit_quality import should_record_observation
    last = {"date":"2026-07-15","expiry":"2026-07-17","short":743.0,"long":733.0,
            "ratio":0.104,"bucket15":39}
    # same candidate, same 15-min bucket, ratio moved < 0.5pp -> do NOT record
    assert should_record_observation(last, date="2026-07-15", expiry="2026-07-17",
        short=743.0, long=733.0, ratio=0.106, bucket15=39) is False
    # ratio moved >= 0.5pp -> record
    assert should_record_observation(last, date="2026-07-15", expiry="2026-07-17",
        short=743.0, long=733.0, ratio=0.110, bucket15=39) is True
    # new 15-min window -> record
    assert should_record_observation(last, date="2026-07-15", expiry="2026-07-17",
        short=743.0, long=733.0, ratio=0.104, bucket15=40) is True
    # strike change -> record
    assert should_record_observation(last, date="2026-07-15", expiry="2026-07-17",
        short=744.0, long=734.0, ratio=0.104, bucket15=39) is True
```
- [ ] **Step 2: Run, expect fail** — FAIL (no `should_record_observation`).
- [ ] **Step 3: Implement** in `credit_quality.py`:
```python
def should_record_observation(last, *, date, expiry, short, long, ratio, bucket15):
    if last is None: return True
    if (date, expiry, short, long) != (last["date"], last["expiry"], last["short"], last["long"]):
        return True
    if bucket15 != last["bucket15"]:
        return True
    return abs(ratio - last["ratio"]) >= 0.005   # 0.5 percentage point
```
- [ ] **Step 4: Wire into orchestrator** — replace the unconditional append at 604-606. Track the last recorded observation per DTE bucket in `state` (new field `credit_obs_last: dict` = bucket → dict). Compute `bucket15 = (minutes since 09:30 ET)//15` from `now`. Only append `round(ratio,4)` to `state.credit_ratio_history[bucket]` (and update `credit_obs_last[bucket]`) when `should_record_observation(...)` is True. Keep the `[-60:]` truncation.
- [ ] **Step 5: Behavior test** — a test running 50 identical entry cycles → history grows by ≤1; a strike change → +1.
- [ ] **Step 6: Run full suite + commit** — `git commit -am "Fix item 3: dedup adaptive credit-ratio history (per 15-min/strike/expiry/0.5pp)"`

---

## Task 4: Markout collector split & batching (item 4)

**Files:** `bot/features.py` (new flag), `bot/research/markouts.py`, `bot/app/orchestrator.py` (`run_markout_cycle`, signal dedup at ~469), `bot/app/run_s2b_alldays.py`. Test: `bot/tests/test_markouts.py`.

- [ ] **Step 1: Add flag** — in `bot/features.py` add `markout_tracking: bool = False` (separate from `regime_shadow_monitor`).
- [ ] **Step 2: Failing test (gating)**:
```python
def test_markout_cycle_gated_on_markout_tracking_not_shadow(make_deps):
    state, deps = make_deps(regime_shadow_monitor=True, markout_tracking=False)
    run_markout_cycle(state, deps, NOW)
    assert deps.markout_writes == []          # shadow on, markout off -> no markout work
```
- [ ] **Step 3: Run, expect fail** — FAIL (today gated on `regime_shadow_monitor`).
- [ ] **Step 4: Re-gate** `run_markout_cycle` and the signal-recording block (~line 464-475) on `deps.features.markout_tracking`. Leave `run_s2b_alldays.py` at `markout_tracking=False`.
- [ ] **Step 5: Failing test (dedup + batch)**:
```python
def test_resolve_due_batches_unique_spreads_into_one_quote_call():
    # 200 pending items across 5 unique (short,long,expiry) spreads
    calls = []
    def batch_quote(symbols): calls.append(tuple(symbols)); return {s: 0.5 for s in symbols}
    tracker = MarkoutTracker(write_fn=lambda r: None, pending=make_200_pending_5_unique())
    tracker.resolve_due_batched(NOW, spy_now=750.0, batch_quote_fn=batch_quote)
    assert len(calls) == 1                     # one batched quote call
    assert len(calls[0]) == 10                 # 5 spreads x 2 legs, deduped
```
- [ ] **Step 6: Run, expect fail** — FAIL (no `resolve_due_batched`).
- [ ] **Step 7: Implement `resolve_due_batched`** in `markouts.py` — collect the unique option symbols across all pending items whose horizon is due, issue ONE `batch_quote_fn(symbols)` call, cache one mark per unique spread, then run the existing `resolve_due` per-item logic reading from that cache (no per-item quote calls). Add a coverage counter (`quotes_requested`, `quotes_missing`) returned/logged.
- [ ] **Step 8: Wire** `run_markout_cycle` to build the batched quote fn from `deps` and call `resolve_due_batched`; keep the whole thing wrapped so it never raises into `tick`.
- [ ] **Step 9: Add signal dedup** — when recording a signal (~469), reuse item-3's dedup key so repeated identical candidates don't each spawn a pending markout.
- [ ] **Step 10: Run full suite + commit** — `git commit -am "Fix item 4: separate markout_tracking flag; dedup + batch quotes off critical path"`

---

## Task 5: Black-Scholes gap-stress model (item 5)

**Files:** Create `bot/portfolio/bs.py`; modify `bot/portfolio/gap_stress.py`, `bot/features.py`, `bot/app/wiring.py`/`orchestrator.py` (supply per-leg IV + rate). Test: `bot/tests/test_gap_stress.py`, `bot/tests/test_bs.py`.

- [ ] **Step 1: Failing test (pricer)** in `bot/tests/test_bs.py`:
```python
def test_bs_put_monotonic_and_known_value():
    from bot.portfolio.bs import bs_put
    # deep ITM put ~ intrinsic; OTM put > 0; monotonic decreasing in spot
    assert bs_put(700, 743, 9/365, 0.20) > bs_put(760, 743, 9/365, 0.20)
    assert abs(bs_put(743, 743, 9/365, 0.20) - 3.72) < 0.6   # ATM ~ 0.4*S*iv*sqrt(T)
    assert bs_put(742.01, 743, 9/365, 0.20) > 0.99            # > intrinsic (0.99)
```
- [ ] **Step 2: Run, expect fail** — FAIL (module missing).
- [ ] **Step 3: Implement** `bot/portfolio/bs.py`:
```python
import math
def _ncdf(x): return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
def bs_put(S, K, T, iv, r=0.04):
    if T <= 0 or iv <= 0 or S <= 0:
        return max(0.0, K - S)
    srt = iv * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * iv * iv) * T) / srt
    d2 = d1 - srt
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)
```
- [ ] **Step 4: Run, expect pass.**
- [ ] **Step 5: Add config** — in `bot/features.py`: `risk_free_rate: float = 0.04`, `gap_iv_shocks: tuple = (0.03, 0.05, 0.10)`, `gap_skew_bump: float = 0.03`, `gap_stress_widen: float = 0.25`, `gap_stress_model: str = "bs"`.
- [ ] **Step 6: Failing test (grid worse than intrinsic)** in `bot/tests/test_gap_stress.py`:
```python
def test_bs_grid_reports_larger_loss_than_intrinsic_for_743_733():
    from bot.portfolio.gap_stress import stressed_spread_loss_bs, stressed_spread_loss
    from bot.features import S2bFeatures
    f = S2bFeatures()
    kw = dict(short_strike=743, long_strike=733, credit=1.39, qty=8, spot=754.88, atr=8.58)
    intrinsic = stressed_spread_loss(**kw, drop_atr=1.5)                 # ~ -320 (profit)
    bs = stressed_spread_loss_bs(**kw, drop_atr=1.5, dte=9, short_iv=0.18, long_iv=0.20, f=f)
    assert bs > intrinsic                                               # BS shows a worse (larger) loss
```
- [ ] **Step 7: Run, expect fail** — FAIL (no `stressed_spread_loss_bs`).
- [ ] **Step 8: Implement** in `gap_stress.py` (keep the old intrinsic fn for comparison/back-compat):
```python
from bot.portfolio.bs import bs_put
def stressed_spread_loss_bs(short_strike, long_strike, credit, qty, spot, atr, drop_atr,
                            dte, short_iv, long_iv, f, wing_width=10.0):
    T = max(dte, 0) / 365.0
    S = spot - drop_atr * atr
    worst = 0.0
    for iv_bump in f.gap_iv_shocks:
        for skew_extra in (0.0, f.gap_skew_bump):          # normal, stressed skew
            sp = bs_put(S, short_strike, T, short_iv + iv_bump + skew_extra, f.risk_free_rate)
            lp = bs_put(S, long_strike,  T, long_iv  + iv_bump,             f.risk_free_rate)
            mid = sp - lp
            nat = mid * (1.0 + f.gap_stress_widen)          # stressed-natural close
            worst = max(worst, mid, nat)
    worst = min(wing_width, max(0.0, worst))
    return (worst - credit) * 100.0 * qty
```
Update `gap_stress_losses`/`gap_stress_ok` to dispatch on `f.gap_stress_model` (`"bs"` → new, `"intrinsic"` → old), threading `dte`/`short_iv`/`long_iv` per position.
- [ ] **Step 9: Wire IV supply** — in `orchestrator`/`wiring`, when assembling positions for gap-stress, attach per-leg IV from the Tradier greeks quote (`mid_iv`); fallback: `vix_last/100 * skew_factor` when greeks missing. Add a fallback unit test.
- [ ] **Step 10: Run full suite + commit** — `git commit -am "Fix item 5: BS reprice + shock-grid gap-stress (worst-cell loss)"`

---

## Task 6: Foreign-position quantity matching (item 6)

**Files:** Modify `bot/portfolio/exposure.py:24-36`; gap-stress book assembly. Test: `bot/tests/test_exposure.py`.

- [ ] **Step 1: Failing test**:
```python
def test_foreign_exposure_counts_quantity_difference():
    from bot.portfolio.exposure import foreign_spy_exposure
    Own = _pos(743,733,"2026-07-24",qty=8)
    Broker = _pos(743,733,"2026-07-24",qty=10, credit=0.0)   # 2 extra foreign
    e = foreign_spy_exposure([Broker], [Own])
    assert e["stop"] > 0     # 2 foreign contracts must contribute, not be fully excluded
    # exactly 2 contracts' worth of conservative full-width stop:
    assert e["stop"] == 10.0 * 100 * 2
```
- [ ] **Step 2: Run, expect fail** — FAIL (current code excludes all 10 on key match → stop 0).
- [ ] **Step 3: Implement** — rewrite `foreign_spy_exposure` to build a per-key owned-qty map and, for each broker spread, take `foreign_qty = max(0, broker_qty - own_qty)`; run `account_spy_exposure` on synthetic foreign spreads carrying `foreign_qty`:
```python
def foreign_spy_exposure(all_broker_spy_spreads, own_positions):
    own_qty = {}
    for p in own_positions:
        own_qty[(p.short_strike, p.long_strike, p.expiry)] = own_qty.get(
            (p.short_strike, p.long_strike, p.expiry), 0) + p.qty
    foreign = []
    for b in all_broker_spy_spreads:
        k = (b.short_strike, b.long_strike, b.expiry)
        fq = max(0, b.qty - own_qty.get(k, 0))
        if fq > 0:
            foreign.append(_Spread(b.short_strike, b.long_strike, getattr(b,"credit",0.0), fq, b.expiry))
    return account_spy_exposure(foreign)
```
(Define a tiny `_Spread` namedtuple/dataclass with the fields `account_spy_exposure` reads.)
- [ ] **Step 4: Include foreign in gap-stress book** — where the gap-stress positions list is built (orchestrator/wiring), append foreign spreads (at `foreign_qty`, credit 0 → full-width) so items 5's grid stresses them too. Add a test asserting foreign qty raises the gap-stress total.
- [ ] **Step 5: Run full suite + commit** — `git commit -am "Fix item 6: quantity-aware foreign matching; foreign positions enter gap-stress book"`

---

## Task 7: Risk-budget reason dataclass (item 7)

**Files:** Modify `bot/portfolio/risk_budget.py:44-95`; `bot/app/orchestrator.py` (caller + logging); `bot/ops/report.py`; decision telemetry in `wiring.py`. Test: `bot/tests/test_risk_budget.py`.

- [ ] **Step 1: Failing test**:
```python
def test_cap_to_budgets_returns_limiting_reason():
    from bot.portfolio.risk_budget import cap_to_budgets, RiskBudgetResult
    r = cap_to_budgets(qty=2, credit=1.20, wing_width=10, expiry="2026-07-24", today="2026-07-15",
                       open_positions=OVER_EXPIRY_BOOK, mark_fn=lambda p: 1.2,
                       risk_equity=72000, f=S2bFeatures())
    assert isinstance(r, RiskBudgetResult)
    assert r.allowed_quantity == 0
    assert r.limiting_budget == "expiry_stop"
    assert int(r) == 0                          # int-compat: existing callers keep working
```
- [ ] **Step 2: Run, expect fail** — FAIL (returns a bare int; no `RiskBudgetResult`).
- [ ] **Step 3: Implement** — add the dataclass with `__int__` returning `allowed_quantity` (back-compat), and have `cap_to_budgets` track which budget produced the minimum `q` (record the limiting name each time `_max_q_for_budget` lowers `q`), returning the populated result:
```python
from dataclasses import dataclass
@dataclass
class RiskBudgetResult:
    allowed_quantity: int
    limiting_budget: str | None
    same_day_stop: float; expiry_stop: float; total_stop: float; total_structural: float
    same_day_limit: float; expiry_limit: float; total_stop_limit: float; structural_limit: float
    def __int__(self): return self.allowed_quantity
    def __index__(self): return self.allowed_quantity
    def __eq__(self, o): return self.allowed_quantity == o if isinstance(o,int) else super().__eq__(o)
```
Compute each budget's cap; the `limiting_budget` is the one whose cap equals the final `q` (and is < `qty`); `None` if unconstrained.
- [ ] **Step 4: Update caller/logging** — orchestrator uses `int(result)` for qty and logs `result.limiting_budget` + the exposure/limit/headroom numbers in the DECISION record (replace the bare `"risk_budget"` reason with `f"risk_budget:{result.limiting_budget}"`). `report.py` surfaces the per-budget breakdown.
- [ ] **Step 5: Back-compat test** — assert every existing `cap_to_budgets` call site still sizes identically (via `int(result)`); run the existing risk-budget tests.
- [ ] **Step 6: Run full suite + commit** — `git commit -am "Fix item 7: cap_to_budgets returns RiskBudgetResult with limiting budget (int-compatible)"`

---

## Final verification (after all tasks)

- [ ] `python -m pytest bot/tests -q` → all green.
- [ ] Bot C behavior diff: run one sandbox tick of `run_s2b_live` (all flags off) before/after the branch; confirm identical trade/decision output (only item 2's partial-fill path may differ, and only on a partial).
- [ ] Re-run the 743/733 gap-stress number under the new BS model; record the before/after in the PR description.
- [ ] Do NOT deploy to real money (per advisor + spec guardrails).

---

## Self-Review

- **Spec coverage:** items 1–7 each map to Task 1–7. ✅
- **Placeholders:** test/impl code shown for every step; item 2b's close block references `run_management_cycle`'s existing close handler (executor locates exact lines). No TBDs.
- **Type consistency:** `round_trip_commission_per_contract`, `should_record_observation`, `resolve_due_batched`, `stressed_spread_loss_bs`, `RiskBudgetResult`, `_Spread` used consistently across tasks.
