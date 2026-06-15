# Results — Intraday Whale-Flow Directional Edge (Path A, Stage 1)

Date: 2026-06-14 · Pre-registration: `research/2026-06-14-intraday-flow-edge-prereg.md` (frozen before any return was computed) · Verdict reached on first and only touch of H2.

## Verdict: **ABANDON.** Stage 1 fails the pre-committed gate on all 5 conditions.

Whale-flow direction does **not** predict intraday underlying direction out of sample on the liquid core a $5–15k cash account can trade. The in-sample signal is a textbook overfit: it vanishes completely OOS. We do **not** proceed to Stage 2, and we do **not** build a flow-following intraday bot on this premise.

## The test, in one table (S1 — aggressive directional sweeps, "follow the whale")

**Tuned on H1** (in-sample): mean 60-min signed return rose monotonically with the premium threshold — $100k → +0.039%, $250k → +0.100%, $500k → +0.124%, **$1M → +0.282%**. The optimizer picked **P_min = $1M** (looked best in-sample). *That upward-with-selectivity pattern is itself the overfit warning sign.*

**Frozen, evaluated on H2** (out-of-sample, n = 685 events, ~355 day-blocks):

| horizon | OOS mean | 95% CI (day-block bootstrap) | t-stat |
|---|---|---|---|
| 30 min | +0.012% | [−0.059, +0.080]% | +0.34 |
| **60 min** | **+0.001%** | **[−0.116, +0.119]%** | **+0.02** |
| 120 min | +0.029% | [−0.139, +0.186]% | +0.39 |
| EOD | −0.050% | [−0.252, +0.152]% | −0.62 |

The H1 edge of **+0.282%** at the 60-min horizon collapsed to **+0.001% OOS** — a ~99.6% decay, indistinguishable from zero. Every CI straddles zero.

## Gate scorecard (all must pass → all failed)

| # | condition | result |
|---|---|---|
| 1 | 60-min OOS mean > 0 with bootstrap CI lower bound > 0 | ❌ mean ≈ 0, CI lower = −0.116% |
| 2 | mean ≥ τ = 0.25% economic floor at ≥1 horizon | ❌ max horizon mean = +0.029% |
| 3 | sign positive at both 60-min and EOD | ❌ EOD is negative |
| 4 | beats shuffled-direction null (p < 0.05) | ❌ **p = 0.492** — exactly coin-flip vs the null |
| 5 | premium-decile monotonic (top ≥ median) | ❌ top decile −0.12% **<** median −0.01% (more premium → *worse*, not better) |

Condition 4 is the cleanest tell: randomizing the trade direction reproduces the signal half the time. There is no directional information in the flow to follow.

## S2 — dark-pool floor prints (secondary family, no tuning)

Following floor/dark-pool prints in their nominal direction is **OOS negative and worsening with horizon**: 60-min −0.13% (t = −1.98), 120-min −0.34% (t = −2.98), EOD −0.23% (t = −2.57); null p = 0.97. The underlying tends to drift **against** the print's nominal direction. This kills "follow the dark pool" as a long-the-direction play. (It hints at a *fade*, but that is a brand-new hypothesis — it would need its own pre-registration and the prior is weak; not pursued here.)

## What this does and does not prove

**Does (decisively):** the most intuitive core premise of the proposed bot — "a large/aggressive options sweep or dark-pool print tells you which way the underlying goes next, intraday" — is **false out of sample**, in both its sweep and floor variants, on liquid names, at 30/60/120-min and EOD. This is the **intraday** horizon, which the prior 2026-06-10 / 2026-06-11 studies never tested; the negative now spans daily **and** intraday. Five independent negatives.

**Does not:** prove no intraday options edge of *any* kind exists. Untouched (and now lower-prior) possibilities: non-directional vol structures (flow predicting a *move*, not a *direction* → straddles), or the speculative dark-pool *fade*. Each is a separate experiment, not a rescue of this one.

## Honesty instruments / limitations

- **n & power:** OOS = 685 S1 events over a ~6-week H2 window, ~355 independent day-blocks; single vol regime. A *positive* here would have needed heavy discounting — but a *negative this clean* (edge at the noise floor, p = 0.49 vs null, anti-monotone premium) is robust to the small sample.
- **Multiple comparisons:** 2 families × 4 horizons; the gate judged S1 at the pre-registered 60-min/EOD only. No hyperparameter search beyond the single pre-declared P_min grid on H1.
- **Underlying, not option P&L:** Stage 1 deliberately measured the *necessary precondition* (directional move). It failed, so the (modeled) Stage-2 option economics — which would only subtract theta and spread costs — cannot rescue it. Correctly, we never ran Stage 2.
- **Liquid core (top 80):** retains ~86% of S1 events; the economically tradeable universe for this account.

## Decision

Stage 1 ABANDON → **do not build the flow-following intraday day-trader.** The cumulative evidence (now 5/5 against model-learnable directional edge in UW flow, daily and intraday) says the durable edge this project has is the **mechanical short-premium / weekend-theta structure (S2b)**, not anything conditioned on flow. Recommended next move: stop adding flow-conditioned challengers, and direct energy toward deploying/sizing the already-validated mechanical S2b — or, if intraday is still the goal, pre-register a *fundamentally different* thesis (non-directional vol) with eyes open that the prior is now weak.

### Artifacts (`research/intraday/`, not committed)
- `pull_bars.py` → `bars/*.parquet` (80 liquid-core tickers, 1-min, Mar 4–Jun 11 2026)
- `build_outcomes.py` → `intraday_outcomes.parquet` (42,861 alert×horizon signed returns; coverage 100% of liquid-core, 307 late-day drops)
- `stage1_test.py` → `stage1_results.json` (H1 tuning, H2 OOS, nulls, bootstrap, decile, gate)
