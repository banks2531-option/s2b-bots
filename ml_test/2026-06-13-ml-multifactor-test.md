# Test A — does a multi-factor ML model beat mechanical S2b out of sample? (2026-06-13)

**Question (the user's thesis).** A "highly intelligent bot combining trend,
whale flow, risk, past performance, and all factors" should beat simple
mechanical rules. Test it honestly: assemble every point-in-time factor we
have on the existing S2b put-spread entries, train a logistic regression and a
gradient-boosted-tree model under a strict walk-forward purged protocol, and
ask whether the model's out-of-sample (OOS) probability can FILTER or SIZE the
mechanical S2b trades to beat unconditioned S2b.

**Answer up front: NO.** Out of sample the model has **zero** discriminating
power — OOS AUC 0.471 (HGB) / 0.521 (logit), statistically indistinguishable
from, and on the tree side *worse than*, its own shuffled-label null
(0.474 / 0.556). The continuous-edge regressor's OOS prediction correlates
**−0.17** with realized P&L (wrong sign). When the model's probability is used
to filter the **real tradable Monday S2b arm** (the only arm with edge, PF
1.86), it **destroys** value: PF 1.86 → 1.67 (HGB) / 1.64 (logit), P&L
$1,709 → $1,254 / $1,241 — it skips winners. Intelligence/factor-stacking did
not manufacture edge on these inputs. This is fully consistent with the two
prior flow studies (2026-06-10 directional, 2026-06-11 overlay), which already
found no systematic signal in the flow data; Test A extends the negative to the
*whole* factor stack (trend, regime, vol/IV, microstructure, recent
performance), not just flow.

Constraints honored: zero Databento; data = existing simulation artifacts +
free yfinance SPY/^VIX daily history (for warm trailing technicals). No droplet
state touched. No keys printed. Not committed.

---

## 1. Dataset construction and leakage controls

**Unit of observation.** Each candidate S2b-style daily put-spread entry in the
existing 246-trade daily panel (`simulations/trades_panel_daily.csv`,
2025-03-04 → 2026-02-27). Daily Mon–Fri entries, SPY bull put spread, short
~40Δ put, $10 wing, weekly expiry, TP 50% / stop 2× / exit 1 DTE — identical
fills to the validated runs. n = 246.

**Labels (both modeled).**
- `win` = 1 if pnl > 0 (binary). Base rate **0.772** (190 wins / 56 losses).
- `pnl_per_risk` = pnl / max-loss-per-contract, where max loss = (10 − credit)·100
  (continuous P&L per $ risked). Mean +0.0044, std 0.282.

**39 features**, all assembled strictly point-in-time as of the 10:00 ET entry:
- **Flow / dark-pool (9):** the existing `flow_features.csv` columns —
  `idx_put_share_am`, `idx_put_ask_prem_am`, `idx_put_sweep_cnt_am`,
  `breadth_bear_am`, `dp_prem_am`, prior-day `idx_put_share_prior` /
  `idx_put_ask_prem_prior`, `dvix`, `alert_cnt_am`. (10th, `dp_idx_sell_share_am`,
  is all-NaN by the prereg rule and dropped.) These are AM 04:00–09:59 ET by
  construction → PIT.
- **Regime (6):** `regime_up`/`regime_down` from `regime_series.csv`,
  `above_sma20`/`above_sma50`, `dist_sma20`/`dist_sma50` (SPY vs 20/50 SMA,
  prior close).
- **Technicals / trend (12):** prior-day, 5/10/20-day SPY returns, RSI(14),
  ATR(14)%, realized vol 5/10/21d (annualized), `gap` (entry-day open vs prior
  close — known at 10:00), `intraday_to_10` (10:00 spot vs open — known),
  `dist_hi20` (distance from trailing-20d high).
- **Vol / IV (5):** prior-day VIX close + change, `iv_prior`,
  `iv_minus_rv_prior`, `ivalert_minus_rv_prior` (from `ivrv_series.csv`, prior
  session).
- **Microstructure of the entry (3):** `credit_width` (credit/10),
  `short_delta`, `dte`.
- **Recent performance (4, PURGED):** trailing-10 win rate and mean
  pnl_per_risk, last trade's pnl_per_risk, trailing equity drawdown — computed
  **only over trades that EXITED strictly before the current entry_date**.

**Leakage controls (the cardinal rule).**
1. *No entry-day lookahead in technicals.* All SPY-daily features
   (returns, SMAs, RSI, ATR, RV, VIX) are read from the last session **strictly
   before** entry_date. The entry-day high/low/close are never used. Only the
   entry-day open (for `gap`) and the 10:00 spot (for `intraday_to_10`) are
   used — both genuinely observable at 10:00. Extended SPY/^VIX history was
   pulled from yfinance back to 2024-09 so 50-day SMA and 21-day RV are fully
   warm by the first 2025-03-04 entry.
2. *Recent-performance purge.* A trade still open at the current entry cannot
   contribute its (unrealized) outcome — only strictly-prior **exits** count.
3. *Walk-forward purge/embargo* (see §2): per fold, any training trade whose
   **exit_date ≥ the first test-block entry_date** is dropped, so an
   overlapping trade open at the fold boundary cannot leak its label.

Warmup NaNs are small and confined to the first ~7 entries (flow gaps,
recent-perf cold start); imputed median (logit) or handled natively (HGB).

## 2. Protocol

- **Walk-forward, expanding window, ONLY** (never random K-fold — trades are
  time-ordered and overlap). Init train = first 45% (≈110 entries); predict the
  next 10 entries; roll. 14 OOS folds. OOS predictions concatenated → **n_OOS =
  136**, covering **2025-08-13 → 2026-02-27** (i.e. essentially all of H2; the
  first 45% consumes H1, so the honest OOS window is the calm second half).
- **Models:** (a) logistic regression — median-impute → standardize → L2 (C=0.5),
  class-balanced; (b) HistGradientBoosting classifier (depth 3, 200 trees, lr
  0.05, min_leaf 20, L2 1.0). Mild regularization deliberately heavy given tiny
  effective n. (xgboost not installed; sklearn HGB is the protocol-sanctioned
  GBT.) Continuous P&L modeled with an HGB **regressor** under the same WF
  protocol for the sizing test.
- **Label-shuffle NULL.** Identical pipeline, train labels permuted within each
  fold. If the shuffled run also "predicts," the CV leaks. (It doesn't — see §3.)

## 3. OOS AUC vs the shuffled-label null (the headline honesty instrument)

| model | OOS AUC (real) | OOS AUC (shuffled null) | OOS log-loss real / null |
|---|---|---|---|
| logistic regression | **0.521** | 0.556 | 0.730 / 0.793 |
| HistGradientBoosting | **0.471** | 0.474 | 0.990 / 0.838 |

n_OOS = 136, OOS base rate 0.735.

- **Both models are at or below 0.50 AUC** — no better than guessing the base
  rate. The HGB model is *below* the coin-flip line.
- **The shuffled-label null is as good or better than the real model** in both
  cases (logit null 0.556 > real 0.521; HGB null 0.474 ≈ real 0.471). The model
  extracts nothing the noise floor of the pipeline doesn't already produce by
  chance. (That the null sometimes scores slightly >0.5 is itself just the
  small-n sampling spread — 136 overlapping observations — and is the honest
  yardstick: the real model lands inside that spread.)
- **Rank-ordering check (the cleanest tell).** OOS win rate by predicted-prob
  quartile is non-monotone and *inverted* for HGB:

  | predicted-prob quartile | HGB OOS win rate | HGB OOS mean P&L | logit win rate |
  |---|---|---|---|
  | Q0 (lowest) | 0.735 | −$23 | 0.735 |
  | Q1 | 0.824 | +$22 | 0.765 |
  | Q2 | 0.676 | −$24 | 0.676 |
  | Q3 (highest conf) | **0.706** | **−$33** | 0.765 |

  The model's *most confident* trades are among its *worst*. There is no signal
  to rank on.
- **Continuous edge.** OOS correlation between the HGB regressor's predicted
  pnl_per_risk and the realized pnl_per_risk = **−0.174** (wrong sign). The
  model's edge estimate is anti-informative OOS.

## 4. The decision-rule backtest (filter / size) vs baselines

### 4a. Filter on the daily panel's own OOS window (matched baseline)

The unconditioned **daily** panel is not the tradable arm — its OOS window
(all-H2) is itself unprofitable (PF 0.84 base / 0.65 at 2×; the daily panel's
documented thin edge sits in H1, which the WF protocol spends entirely on
training). Threshold chosen on each fold's TRAIN PF (keep ≥50%), frozen, applied
OOS:

| costs | model | UNCOND OOS PF / P&L / DD | FILTER OOS PF / P&L / DD |
|---|---|---|---|
| base | logit | 0.839 / −$1,962 / $3,549 | 0.859 / −$1,114 / $1,885 |
| base | HGB | 0.839 / −$1,962 / $3,549 | 0.800 / −$2,291 / $2,938 |
| 2× | logit | 0.650 / −$4,119 / $4,836 | 0.684 / −$2,354 / $2,601 |
| 2× | HGB | 0.650 / −$4,119 / $4,836 | 0.629 / −$4,091 / $4,175 |

The logit filter nudges PF up by ~0.02–0.03 (still **< 1.0**, still a losing
window) mostly by cutting trade count and thus shrinking the drawdown; the HGB
filter makes PF *worse*. Neither turns the window profitable. This is not edge —
it is a marginal variance reduction on a losing book.

### 4b. Filter the REAL tradable Monday S2b arm (the comparison that matters)

All 47 Monday S2b trades (PF 1.86, P&L $1,709) are a subset of the daily panel,
so each gets a leak-free OOS probability from a model that never trained on it.
25 of the 47 fall in the OOS window and are scored; the 22 pre-OOS Mondays are
taken as-is (deployment rule: no model signal → trade normally). Frozen rule:
skip the bottom-third predicted-prob scored Mondays.

**Baselines:** unconditioned S2b PF **1.86** base / **1.39** at 2×, P&L
**$1,709**, maxDD **$481**; validated 10%-sizing config (scaling study) ≈
$310/mo on $20k.

| model | costs | UNCOND PF / P&L / DD (H1/H2) | FILTER PF / P&L / DD (H1/H2) | skipped |
|---|---|---|---|---|
| HGB | base | 1.859 / $1,709 / $481 (1.77/2.00) | **1.674 / $1,254 / $481 (1.77/1.51)** | 8 of 47 |
| HGB | 2× | 1.393 / $861 / $501 (1.42/1.35) | 1.302 / $615 / $501 (1.42/1.09) | 8 |
| logit | base | 1.859 / $1,709 / $481 (1.77/2.00) | **1.635 / $1,241 / $481 (1.77/1.43)** | 8 of 47 |
| logit | 2× | 1.393 / $861 / $501 (1.42/1.35) | 1.251 / $531 / $522 (1.42/**0.98**) | 8 |

Every filter cut **lowers both PF and P&L** on the tradable arm. The model skips
profitable Mondays: −$455 (HGB) / −$468 (logit) of base-cost profit thrown away,
and PF falls in H2 (the OOS half) every time. At 2× costs the logit filter pushes
H2 PF below 1.0. The filter does not improve drawdown either (same $481, because
the worst Monday is not among the skipped set — the model can't find it). The H1
columns are unchanged because no H1 Mondays are in the OOS window; the "improvement"
the user might hope for can only come from H2, and there it is strictly negative.

### 4c. Sizing by predicted edge

HGB-regressor edge-sizing (1.5×/1.0×/0.5× by predicted-edge tercile) on the 25
scored Mondays moved PF 1.49 → 1.72 and P&L $570 → $715 — but the correlation
between predicted edge and realized P&L on those 25 trades is **−0.02**
(zero/negative). That PF bump is multiple-comparisons noise from a 25-trade
sample with no underlying signal, not a sizing edge. Reported so it is not
mistaken for one.

## 5. Feature importance (and whether the "important" features are lookahead-prone)

OOS-style permutation importance (60/40 holdout, AUC drop on the held-out 40%;
holdout base AUC 0.373 — itself **below 0.5**, i.e. the model overfits the
holdout too):

| rank | feature | AUC drop | lookahead-prone? |
|---|---|---|---|
| 1 | rsi14 | +0.015 | no (prior close) |
| 2 | idx_put_share_am | +0.015 | no (AM flow) |
| 3 | idx_put_share_prior | +0.010 | no |
| 4 | recent_last_pnl | +0.010 | no (purged) |
| 5 | ret_5d | +0.009 | no |
| 6 | recent_dd | +0.006 | no (purged) |
| 7 | dist_sma50 | +0.006 | no |
| 8 | iv_prior | +0.004 | no |
| … | (everything else) | ≤ +0.002 | — |

Two honest readings: (a) the importances are **tiny** (largest AUC drop 0.015,
on a model that scores below 0.5 OOS) — there is no feature with real lift to
rank; (b) reassuringly, the top features are **not** the lookahead-prone ones
(no entry-day high/low/close, no future-leaking field surfaces), which confirms
the leakage controls held — the model simply has nothing to learn. A model that
*had* leaked would show a same-day price/outcome feature dominating with a large
drop and a shuffled-null AUC near 0.5; we see neither.

## 6. Honesty instruments

- **Effective independent n is tiny.** 246 entries, but mean hold ≈ 3.8 calendar
  days with daily entries → heavy overlap. Non-overlapping blocks ≈ **96**, and
  even that overstates independence (same-week trades share one SPY path; the
  OOS window is a single ~6-month H2 regime block). The Monday filter test rests
  on **25 scored trades**. Any apparent improvement at this n is noise until
  proven on fresh data — the exact trap (in-sample PF 5–7 → OOS 0.5–0.8) that
  destroyed the legacy project.
- **Multiple comparisons.** Models/configs tried = **2 classifiers** (logit,
  HGB) + 1 regressor, each with: a real run, a shuffled-null run, two
  cost levels, and two decision rules (filter, size) on two trade sets
  (daily panel, Monday arm). ~16 decision-relevant backtest cells. No
  hyperparameter search was run (regularization fixed a priori for small n) —
  deliberately, to avoid manufacturing a winner. The one "positive" cell
  (4c edge-sizing) is explained by its own −0.02 signal correlation = noise.
- **Shuffled-null beside every headline metric** (§3): the null matches or beats
  the real model, the decisive evidence that there is no signal *and* no leak.

## 7. Verdict

**Does an ML multi-factor model beat mechanical S2b out of sample? NO.**

Out of sample the model cannot tell winning S2b entries from losing ones (AUC
0.47–0.52, ≤ its shuffled-label null; predicted edge correlates −0.17 with
realized P&L; most-confident trades are the worst). Used as the user intends —
to filter or size the mechanical trades — it **subtracts** value on the only arm
that has edge: the real Monday S2b PF falls from 1.86 to 1.64–1.67 and annual
P&L from $1,709 to ~$1,250, because the model skips winners it mistakes for
losers. Nothing beat unconditioned S2b, the 10%-sizing config, the H1/H2 splits,
or survived 2× costs.

Plainly: **intelligence and factor-stacking did not manufacture edge on these
inputs.** Trend, regime, vol/IV, microstructure, recent performance, and whale
flow — combined in a logistic model and a gradient-boosted-tree model under a
leak-controlled walk-forward protocol — add no out-of-sample discrimination over
the mechanical rule. This is consistent with the 2026-06-10 directional flow
study (5/5 pre-registered hypotheses failed OOS) and the 2026-06-11 overlay
study (0/9 features passed): the edge in S2b is the mechanical short-premium /
weekend-theta structure itself, not anything a model can condition on. The
honest caveat is n: this is a 6-month OOS window of overlapping trades
(~25 scored Monday decisions); a *positive* result here would have needed
heavy discounting, but a *negative* result this clean — model at/below its own
noise floor, filter strictly value-destroying — is robust to the small sample.
The recommendation is unchanged: trade mechanical Monday-only S2b at sane
sizing; do not deploy an ML filter/sizer on these factors.

### Artifacts (`ml_test/`, not committed)
- `build_features.py` → `features.csv` (246 × 39 features + 2 labels + meta)
- `fetch_spy_history.py` → `spy_daily.csv` (yfinance SPY/^VIX daily, warm lookback)
- `run_ml_test.py` → `ml_results.json`, `oos_predictions.csv` (HGB),
  `oos_pred_logit.csv`, `oos_reg.csv` (regressor OOS edge)
- `backtest_filter.py` → `backtest_results.json` (daily-panel filter, both costs)
- `backtest_monday.py` → `monday_filter_results.json` (real Monday arm, both costs)
