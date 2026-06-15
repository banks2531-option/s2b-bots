# Large-Whale Flow-Driven Strategy Test — Results

Date: 2026-06-14 · Code: `research/broad/whale_strategy_test` (built on cached `intraday_outcomes.parquet`). Question: can large-whale UW flow as the main driver + risk-factor gates, all days, reach PF 2.0?

## Verdict: NO — and it is the cleanest demonstration of the in-sample→OOS trap in the project.

42,861 large-whale entries (sweeps + floor prints), all five weekdays, top-80 liquid core; trend-alignment risk gate (trade direction agrees with intraday move so far). Premium threshold tuned on H1, frozen, tested on H2.

**H1 (in-sample) — tuning picked ≥$1M because it looked best:**
| whale size (aligned) | H1 gross-PF |
|---|---|
| ≥ $250k | 1.56 |
| ≥ $500k | 1.59 |
| **≥ $1M (selected)** | **2.04** |

**H2 (out-of-sample), same frozen rule, all days:**
| metric | 60-min | EOD |
|---|---|---|
| win rate | 41.9% | 49.5% |
| mean move | −0.047% | −0.054% |
| **gross-PF** | **0.89** | **0.91** |

**PF 2.04 in-sample → 0.91 out-of-sample.** The gated large-whale entries lost on the underlying move OOS. The in-sample 2.04 was pure threshold-tuning overfit — the same in-sample-high → OOS-low mechanism that killed the legacy bot.

## Why PF 2.0 is mathematically off the table here
Gross-PF (underlying-move, no costs, no capped upside) is a **ceiling** on any defined-risk option version — costs only subtract. OOS ceiling = **0.91 < 1.0**, so a tradeable option strategy following this signal has PF well below 1.0. PF 2.0 net is impossible from this signal.

## Bonus confirmation
Bigger whales did **worse** OOS: whale≥$1M+aligned (0.91) underperformed all-entries (1.06). "Focus only on large whales" points the wrong way — the 7th independent confirmation that flow direction is not an edge here, consistent with bot B's logs (losers carried 3× more whale premium).
