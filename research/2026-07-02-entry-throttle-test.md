# Intraday Entry-Throttle Test — Result

Date: 2026-07-02 · Script: `research/throttle/throttle_test.py`
Prompted by the 7/2 sell-off: *does skipping entries on weak/vol-spiky days beat trading every day, net
of forgone premium?* Tested on the REAL all-days S2b trade ledger (`simulations/trades_panel_daily.csv`,
246 trades, 2025-03..2026-02, all weekdays, from the Polygon-intraday engine) + its 2x-slippage twin.
Throttle signals are look-ahead-free (ledger `spot` = SPY at the 10:00 ET entry; `sigma` = entry IV).

## Baseline: the all-days book is barely an edge, and the per-weekday split is the real story
| | PF | total | win% | avg/trade |
|---|---|---|---|---|
| all-days, base slippage | **1.03** | +$650 | 77% | +$2.6 |
| all-days, 2x slippage | **0.76** | −$5,519 | 74% | −$22.4 |

**Per-weekday PF (base slippage): Mon 1.86, Tue 0.80, Wed 1.08, Thu 0.71, Fri 1.42.** Monday IS the
validated edge (PF 1.86, matching the OOS number); **Tue/Thu are net losers that dilute all-days to
PF ~1.03, and to a LOSS (0.76) under cost stress.** This is the whole all-days-vs-Monday story, quantified
on real trades. The 7/2 drawdown came from all-days selling on a structurally weak non-Monday, not from a
missing intraday signal.

## The throttle FAILS — and skipping weakness often skips WINNERS
| Throttle | skip | kept PF | skipped avg $ | null p |
|---|---|---|---|---|
| gapdown >0.30% | 56 | 1.06 | −$5.0 | **0.36** |
| gapdown >0.50% | 39 | 0.96 | **+$32.4** | 0.82 |
| gapdown >0.75% | 25 | 0.98 | **+$43.3** | 0.85 |
| volhigh IV≥0.25 | 28 | 1.01 | +$18.3 | 0.63 |
| volhigh IV≥0.28 | 18 | 1.05 | −$11.3 | 0.37 |
| trendoff (<200dMA) | 42 | 0.95 | **+$34.9** | 0.87 |

- **No variant beats a random skip of the same N trades** (every null p ≫ 0.05). The signal has no skill at
  selecting losing entries.
- **The aggressive gap-down and trend-off throttles skip WINNERS** (skipped-trade avg +$32 to +$43),
  *lowering* kept-book PF. Selling defined-risk put spreads INTO morning weakness is *profitable* on
  average — elevated IV means richer credit, and the index tends to mean-revert. That is the variance-risk
  premium working as designed: you're paid most to sell insurance when others are scared. The wing +
  stop is what makes selling into fear safe.
- The one marginally-positive variant (gapdown>0.30%, PF 1.03→1.06) **fails the null (p=0.36) and reverses
  out-of-sample**: H1 throttled +$1,451 (≈baseline) but H2 throttled −$2,065 vs −$1,042 baseline. Overfit.

## OOS split confirms the reversal
gapdown>0.50%: H1 PF 1.17→1.19 (marginal), **H2 PF 0.90→0.79 (worse)**. volhigh: no high-IV days in H2 → no
effect. The apparent H1 benefit does not survive to H2.

## Verdict
**There is no working intraday entry-throttle.** Skipping weak/vol-spike days doesn't beat random skipping,
and skipping *gap-downs* actively removes winners — the opposite of the "don't sell into the knife"
intuition. This is the **4th failure of IV/vol-conditioned entry** in this project (after the 3 prior IVR
failures). The counterintuitive, economically-sound truth: for a *defined-risk* premium seller, morning
weakness is a GOOD entry (richest premium), and the stop — not entry avoidance — is the correct protection.

The 7/2 case specifically (SPY opened HIGH at 751, reversed to 740 in the afternoon) is an **afternoon
reversal after entry** — no entry-skip signal can catch it; only the stop (which fired, capping ~$4,900 on
one Bot B spread) and de-gross address it.

**The real, data-backed lever is NOT an intraday trick — it's the day-of-week:** Monday-only PF 1.86 vs
all-days 1.03 (0.76 after 2x costs). Dropping the structurally-weak non-Monday days (esp. Tue/Thu) is the
validated way to lift the book; the all-days experiment (Bot B) is confirmed to dilute the edge and fail
the cost stress. Caveat: this 1-year sample contains no sustained bear, so the trend-off result does NOT
retire the 200d gate (its value is in 2008-style crashes absent here).
