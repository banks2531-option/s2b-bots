# Pre-Registration — Intraday Whale-Flow Directional Edge (Path A, Stage 1→2)

Date written: 2026-06-14 · Author: Shawn (with Claude) · Status: PRE-REGISTERED (no results yet)
Decision date: **2026-06-21** · Lineage: extends the 2026-06-10 directional-flow study and 2026-06-11 overlay study (both found **no daily-horizon OOS edge**) to the **intraday horizon**, which neither tested.

> This file is written **before** looking at any outcome. H2 (out-of-sample) is touched exactly once, after every choice below is frozen on H1. No iterating on H2. This is the discipline (C4/C5) whose violation produced the legacy bot's in-sample PF 5–7 → live 0.5–0.8 collapse and Test A's AUC 0.47.

> **Revision log (2026-06-14, pre-results — returns never inspected):** the original S1/S2 and universe definitions were written before checking field-value distributions and were malformed (`all_opening==True` is only 1,519 rows and disjoint from sweeps → S1 was empty; `in_cluster` covers 75% of rows → S2 non-selective). Definitions below are corrected against **field structure only** (no outcome data touched). Universe restricted to a liquid core for both economic correctness (cash account trades liquid names) and the Polygon free-tier 5-req/min limit.

---

## 0. The question

A bot that "acts as an options day trader" must, at minimum, be able to predict **intraday** direction after a whale-flow event. Daily-horizon flow prediction is already disproven here (06-10: 5/5 hypotheses failed OOS; 06-11: 0/9 features passed). The **untested** question:

**After a qualifying whale-flow alert at time T, does the underlying move in the alert's direction over the next minutes-to-hours, out of sample, by enough to plausibly clear 0–2 DTE option costs?**

If **no** → STOP. No option structure rescues a directional signal that does not exist. If **yes** → Stage 2 models whether the edge survives option theta/spreads, then (separately) we build a paper bot.

This is deliberately the **cheapest possible kill**: equity-only, signal already on disk, decisive in days.

## 1. Hypotheses (pre-registered, directional)

- **H1a (primary):** A frozen "aggressive-opening-sweep" signal produces mean signed intraday underlying return > 0 on H2 at the 60-min horizon, with a block-bootstrap 95% CI lower bound > 0.
- **H1b (economic):** That mean signed return is ≥ **τ = 0.25%** (the pre-registered floor below which a 0–2 DTE defined-risk spread cannot plausibly profit net of costs) at ≥1 horizon.
- **H1c (robustness):** Sign is positive at **both** the 60-min and EOD horizons, and the real edge exceeds its shuffled-direction null (real > 95th pct of null draws).
- **Null:** signed intraday return is ≤ 0 or < τ or indistinguishable from the direction-shuffled null. **Default belief is the null** — four prior negative results set a strong prior.

## 2. Data

| Source | Use | Status |
|---|---|---|
| `research/alerts_panel.parquet` (695,950 alerts, 2026-03-04→06-10, 82 days, H1/H2 split ~348k each) | the flow events + features + timestamps | on disk ✓ |
| Polygon 1-min **equity** aggregates (key in `engine_v345.py:57`) | intraday underlying price path after each alert | confirmed working ✓ (200 OK on SPY 1-min) |
| Polygon **option** intraday | NOT used Stage 1; Stage 2 only if entitled | unconfirmed / likely unentitled ✗ → Stage 2 models option P&L instead |

**Do NOT use** the panel's `fret1/3/5`, `xret1/3/5` columns — those are **daily** underlying returns and are the already-failed 06-10/06-11 experiment. Using them = repeating a dead study.

**Universe filter U (fixed, not tuned):** `is_index == False`; `dir != 0`; ticker in `prices.parquet` (studied liquid universe); **liquid core = top 80 tickers by S1∪S2 candidate-event count** (these are the only names a $5–15k cash account can day-trade with acceptable option spreads — SPY/QQQ/NVDA/MU/TSLA/AMD/SMH/GLD/MSFT/META/AVGO/… ; top-80 retains ~86% of S1 events vs full liquid set, top-200 adds <8% more). Pull per-ticker **full-range 1-min equity bars** (paginated), rate-limited to Polygon's 5 req/min, cached to parquet. ~240 calls total.

## 3. Signal definition (the ONLY tuned quantity is P_min, fit on H1, frozen for H2)

Two pre-registered signal families (cap of 2 to bound multiple comparisons), grounded in actual field semantics:

- **S1 — Aggressive directional sweep (primary, "follow the whale"):** `has_sweep == True` AND `side == 'ask'` (aggressive buyer lifting the offer) AND `dir != 0` AND `total_premium ≥ P_min`. Direction `d = dir` (+1 bullish / −1 bearish). Liquid-core base ≈ 3,700 events at P_min=$500k (H1≈1.6k / H2≈2.0k).
- **S2 — Dark-pool floor prints (secondary, "the dark-pool data"):** `has_floor == True` AND `dir != 0`. Direction `d = dir`. ≈ 1,700 liquid-core events (H1/H2 ≈ 1.0k/0.7k). Genuinely selective (floor prints are only 5,229 of 695k rows).

**Tuning protocol:** the ONLY tuned quantity is S1's `P_min`, chosen **on H1 only** over the fixed grid **{\$100k, \$250k, \$500k, \$1M}** as the value maximizing H1 mean signed 60-min return subject to ≥ **500** H1 events (power floor). Frozen, applied to H2. S2 has no tuned parameter. **Nothing else is tuned;** no other hyperparameter search. Ties → larger P_min (fewer, higher-conviction events).

## 4. Stage 1 outcome construction (leakage-controlled)

For each qualifying alert at `ts_et` on date D:
1. **Entry** = open of the **first 1-min bar starting ≥ ts_et + 60 s** (a realistic reaction lag; never the same instant as the alert — no lookahead).
2. **Exit prices** at entry + 30 / 60 / 120 min, and at **15:59 ET** (EOD). Signed return `r_h = d · (P_exit_h / P_entry − 1)`.
3. Drop alert if entry bar missing, or if horizon extends past 16:00 (EOD horizon always truncates at 15:59; intraday horizons past close are dropped, logged).
4. Log coverage: # alerts dropped for missing bars / late-day truncation (survivorship transparency).

**Metrics per family, per horizon, split H1 (fit) / H2 (OOS):** mean & median `r_h`; share `r_h > 0`; mean net of a fixed haircut.

## 5. Honesty instruments (mandatory, from the Test A playbook)

1. **Shuffled-direction null:** randomize `d` within the qualifying set, recompute — real edge must beat the 95th percentile of the null distribution.
2. **Block bootstrap by (ticker, day):** intraday alerts are massively overlapping/clustered; CIs use day-block resampling, not naive per-alert. Report **n_events** AND **n_independent day-blocks**.
3. **Premium-decile monotonicity:** signed return by `total_premium` decile — a real "follow the whale" effect should strengthen with size; a flat/non-monotone profile is evidence of noise.
4. **Multiple-comparison ledger:** decision cells = 2 families × 4 horizons × {real, null} = 16. H1a/H1b/H1c judged on the **primary family S1 at the pre-registered 60-min/EOD horizons only**; everything else is descriptive.
5. **H2 touched once.** Any post-hoc idea spawned by H2 is a *new* pre-registration, not a result of this one.

## 6. PASS / ABANDON gate (Stage 1)

**PASS → Stage 2** requires ALL of, on **H2 (OOS)**, primary family **S1**:
1. mean 60-min `r_h > 0` with block-bootstrap 95% CI lower bound **> 0**;
2. mean `r_h ≥ τ = 0.25%` at ≥1 horizon (economic floor);
3. sign positive at **both** 60-min and EOD;
4. real > 95th pct of shuffled-direction null;
5. premium-decile profile not inconsistent with monotonic (top decile ≥ median decile).

**ABANDON** if S1 fails. If **S2** passes but S1 fails → treat as **weak/borderline**: document, do **not** auto-promote; at most one pre-specified extension (forward paper collection of S2), default stop. Anything ≤ 2 of 5 on S1 → clean kill, write the negative up, done.

## 7. Stage 2 (run ONLY on PASS) — does the edge survive option economics?

Model a **defined-risk 0–2 DTE structure** in direction `d` (debit vertical, width sized to the cash account): price entry/exit with Black-Scholes using `iv_start` and the **measured** intraday underlying path from Stage 1; apply a realistic option round-trip cost (modeled half-spread = max(5% of width, $0.05/contract leg)); compute per-trade P&L distribution, PF, win rate, left tail, under settled-funds cadence (N new positions/day ≤ settled cash / risk-per-trade) and $5–15k sizing. **Stage 2 PASS bar (provisional, finalized if reached):** OOS net **PF ≥ 1.3**, positive mean P&L after costs, worst-trade tail tolerable at chosen size. Option P&L here is **modeled, not measured** — explicitly flagged as an approximation; a Stage-2 pass means "worth a *forward paper* test," not "deploy."

## 8. Known limitations (stated up front, not as excuses later)

- **Short history:** ~3 months, H2 OOS ≈ 6 weeks. Low power; a *positive* result needs heavy discounting, a *clean negative* is robust.
- **Modeled option P&L** in Stage 2 (no historical option quotes on this key).
- **Single vol regime** in the window; no claim of cross-regime generality.
- **Polygon rate limits / coverage gaps** handled by caching + logged drops; coverage reported.

## 9. Artifacts (to be produced)

- `research/intraday/pull_bars.py` → cached `research/intraday/bars_<ticker>_<date>.parquet` (or one consolidated store)
- `research/intraday/build_outcomes.py` → `intraday_outcomes.parquet` (alert × horizon signed returns + coverage log)
- `research/intraday/stage1_test.py` → `stage1_results.json` (H1 fit, H2 OOS, nulls, bootstrap, decile, ledger)
- `research/2026-06-14-intraday-flow-edge-results.md` → the verdict (PASS→Stage 2, or ABANDON), written against this prereg
- Stage 2 (conditional): `research/intraday/stage2_option_model.py` → `stage2_results.json`

## 10. Decision rule, in one line

Run Stage 1 → judge S1 against §6 on H2 → **PASS** opens Stage 2 (modeled option economics) → Stage-2 pass opens a *forward paper* bot (Path C, one validated play). Any earlier failure = write the negative, stop. Decision by **2026-06-21**.
