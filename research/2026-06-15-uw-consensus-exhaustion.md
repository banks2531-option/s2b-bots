# UW Community-Consensus Playbook — Exhaustively Tested

Date: 2026-06-15 · Prompted by a user-supplied "algo-community consensus" guide to using Unusual Whales (Vol/OI>1 conviction, premium floors, RepeatedHits ascending fills, flow-as-filter, GEX cross-reference). Code: `research/broad/precise_uw_test.py`, `research/broad/gex_gate_test.py`. All on cached data + a one-time UW GEX pull.

## Verdict: all five consensus elements tested; none yields a tradeable OOS directional edge.

| element | test | OOS result |
|---|---|---|
| Premium thresholds ($50k–$1M) | Stage-1, whale test | higher premium → **worse** OOS |
| Vol/OI > 1 (new positions) | precise_uw_test | doesn't rescue |
| RepeatedHits Ascending Fills | precise_uw_test | gross-PF **1.68 in-sample → 0.90 OOS** |
| Flow as filter, not driver | 2026-06-11 overlay | 0 of 9 flow features added value |
| **GEX cross-reference** | gex_gate_test | **consensus claim refuted (below)** |

## Precise signal (Vol/OI>1 + premium + RepeatedHitsAscendingFill, directional)
20,225 qualifying alerts, top-80 liquid core, H1-tuned / H2-tested. In-sample gross-PF rose with premium to **1.68** at ≥$1M; frozen OOS → **gross-PF 0.90 (60-min) / 0.96 (EOD)**, win rate 42–48%. Same in-sample→OOS collapse. The conviction filters did not change the outcome.

## GEX cross-reference (the "most lucrative" lever per the source)
Pulled SPY/QQQ/IWM historical daily greek-exposure from UW (`/api/stock/{t}/greek-exposure`, 250 days). Net GEX = call_gamma + put_gamma. **Consensus claim:** negative GEX (dealers short gamma) → moves amplified → trade momentum/flow.

OOS (H2), flow following direction, gated by SPY net-GEX regime:
| regime | n | win rate | mean | gross-PF |
|---|---|---|---|---|
| **NEG-GEX** (source: trade momentum) | 10,363 | 49.5% | **−0.063%** | **0.92** |
| POS-GEX (pinning) | 10,810 | 51.2% | +0.149% | 1.24 |
| ungated | 21,173 | 50.4% | +0.045% | 1.06 |
| whale≥500k × NEG-GEX | 899 | 53.8% | +0.080% | 1.13 |

**The consensus is backwards in this data:** following flow in the negative-GEX "momentum" regime *lost* (gross-PF 0.92). Positive-GEX (pinning) was mildly better (1.24) — but that is (a) the *opposite* of the claim, (b) the underlying-move *ceiling* (option costs drag it < 1.0), (c) a single unvalidated post-hoc cut, and (d) far below the >>2.0 a net PF-2.0 strategy needs.

Notable regime context: SPY was negative-GEX every day in March (the live run-up's momentum/decline window) and flipped positive in April (the rally/giveback) — consistent with GEX tracking the same regime as VIX/direction, i.e. not adding independent predictive value.

## Conclusion
The community-consensus UW playbook — including the GEX overlay it calls the most lucrative — does not produce a tradeable directional edge in this data. Combined with the 7 prior flow negatives, **the flow lever is exhausted.** The durable, scalable edge remains mechanical S2b (`docs/specs/2026-06-15-s2b-execution-bot-design.md`).

GEX data retained at `research/broad/gex/` for any future *structural* (non-directional) study.
