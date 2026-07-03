# Hunt for a ≥1.5 PF Non-Monday Edge to Couple with the Monday Bot — Result

Date: 2026-07-02 · Scan: all `simulations/trades_*.csv` real Polygon-data ledgers, sliced by day-of-week;
OOS(H1/H2)+2x-cost check on the two live candidates. Goal: find a mechanical structure for Tue–Fri
clearing PF ≥ 1.5 to pair with validated SPY-Monday S2b.

## Scan: every already-simulated structure × underlying × day (put spreads, 0DTE, condors, butterflies,
## 45-DTE, across SPY/QQQ/IWM/META/TSLA)
Non-Monday cells (n≥12) ranked by PF — the only ones ≥1.5 are flukes:
| ledger | sym | day | n | PF | note |
|---|---|---|---|---|---|
| ho_C_..._noiv_h1 | META | Wed | 15 | **1211** | in-sample holdout, 15 trades — spurious artifact, reject |
| abl_no_iv | IWM | Fri | 13 | 1.54 | ablation config, n=13 — too small, not a deployment |
| **panel_daily** | **SPY** | **Fri** | **50** | **1.42** | the best CREDIBLE non-Monday cell — still <1.5 |
| scale_v2_mwf | SPY | Fri | 49 | 1.36 | |
| ho_A_..._h2 | TSLA | Tue | 16 | 1.26 | small n |
| panel_daily | SPY | Wed | 49 | 1.08 | marginal |
| panel_daily | SPY | Tue | 52 | 0.80 | LOSER |
| panel_daily | SPY | Thu | 48 | 0.71 | LOSER |
| sl_s5_0dte | SPY | Tue–Thu | ~50 | 0.17–0.86 | 0DTE all days LOSE |

**No credible non-Monday day reaches 1.5 PF.** SPY-Friday (1.42) is the best real runner-up; Tue/Thu are
outright losers; 0DTE fails every day.

## OOS + cost gate on the two live candidates (vs the SPY-Monday benchmark)
| candidate | base PF | H1 / H2 | **2x-cost PF** | 2x H1 / H2 |
|---|---|---|---|---|
| **SPY Monday (benchmark)** | 1.86 | 1.77 / **2.00** | **1.39** | 1.42 / **1.35** |
| SPY Friday | 1.42 | 1.51 / 1.32 | **0.98** (dead) | 0.88 / 1.13 |
| QQQ Monday | 1.73 | 2.64 / **1.24** (decays) | 1.13 | 1.53 / **0.86** (neg) |

- **SPY Monday is uniquely robust:** stable across OOS halves (1.77→2.00) AND survives 2x costs (1.39,
  both halves >1.3). It's the only cell that passes every gate.
- **SPY Friday dies on cost:** base 1.42 but **PF 0.98 after 2x slippage** — a fair-fills-only edge.
- **QQQ Monday (same structure, other underlying) also fails the full gate:** OOS-decays (H2 1.24) and
  **goes negative in H2 under cost (0.86).** Not robust to 1.5.

## Verdict
**No — there is no ≥1.5 PF non-Monday edge among any structure, underlying, or day already tested, and none
that survives the OOS+cost gate.** The variance-risk premium this bot harvests is **structurally a
SPY-weekend/Monday phenomenon** (richest weekend theta + Monday mean-reversion + SPY's deepest, most-
overpriced put skew); it does NOT generalize to other weekdays (Tue/Thu are losers, Fri is cost-fragile)
or cleanly to other underlyings' Mondays (QQQ-Monday decays under the full gate). This is consistent with
the project's whole history: the edge is narrow and specific, and every attempt to broaden it dilutes or
fails OOS/cost.

## What to do instead (to deploy more capital at ≥1.5 PF)
Don't add fragile other-day trades. The robust lever is **SIZE on the Monday edge** — the scaling study
shows SPY-Monday PF holds 1.73–1.89 from sz5 to sz25 (more contracts, same edge, same low drawdown). Run
SPY-Monday well (correctly sized + the trend-gate/de-gross defense) and scale it as the account grows,
rather than coupling it with a thinner, cost-fragile second strategy.

## Remaining honest hunts (untested, low prior — offer, don't assume)
The credit-premium family is now exhaustively covered. Genuinely different axes NOT yet tested: (1)
**turn-of-month** entries (calendar-day, not weekday — could add volume without weekday dilution);
(2) **post-vol-event** premium selling (day after FOMC/CPI IV-crush). Both need new sim runs and carry this
project's low prior (most non-Monday-VRP ideas have failed). Not worth deploying without clearing the same
OOS+null+cost gate.

Caveats: 1-year sample, no sustained bear; scan spans many cells so treat any single high-PF cell as
multiple-testing-suspect until it clears the gate (which none of the non-Monday candidates did).
