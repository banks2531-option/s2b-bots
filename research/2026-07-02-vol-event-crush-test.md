# Post-Vol-Event (IV-Crush Harvest) Test — Result

Date: 2026-07-02 · Script: `research/throttle/vol_event_test.py`
Hypothesis: being SHORT premium THROUGH a scheduled macro vol event (FOMC 2pm decision / CPI 8:30 release)
harvests the post-event IV crush and lifts the S2b put-spread's PF; also tested the "enter the day AFTER"
(post-crush calm) variant. Event dates: exact FOMC announce Wednesdays + confirmed 2025 CPI releases (Oct'25
CPI unpublished — govt shutdown; Sep'25 rescheduled to Oct 24). Tagged each REAL all-days ledger trade by
whether its holding window [entry..exit] spanned an event. Null = same-size random subset (5000 draws).

## Result — the crush-harvest FAILS, and the effect reverses
**Base slippage** (baseline all-days PF 1.03):
| bucket | n | PF | total | null p |
|---|---|---|---|---|
| **spanned any event (short through crush)** | 72 | **0.93** | −$446 | 0.66 |
| spanned FOMC only | 35 | 1.00 | −$14 | 0.55 |
| spanned CPI only | 40 | **0.83** | −$627 | 0.73 |
| entered day-AFTER event (post-crush) | 18 | 0.91 | −$147 | 0.60 |
| **NON-event control** | 160 | **1.11** | +$1,329 | 0.30 |

- **Being short premium through FOMC/CPI is WORSE, not better** (PF 0.93 vs 1.11 for non-event days). The
  event's directional/gap risk (a hawkish FOMC or hot CPI gaps SPY) cancels or outweighs the IV-crush
  benefit. CPI-spanning is actively harmful (0.83).
- **No bucket beats random** (every null p 0.44–0.73) — no distinguishable signal.
- **The "day-after" post-crush entry is no edge either** (0.91).
- **OOS reverses:** spanned-event H1 PF 1.24 (+$649) → **H2 PF 0.70 (−$1,095)**. Even the mediocre result
  doesn't hold out-of-sample.

**2x slippage:** everything loses (PF 0.69–0.80), no differentiation between event and non-event buckets.

## Verdict
**No ≥1.5 PF edge — the post-vol-event IV-crush harvest is disproven, and the mechanism runs backwards:**
holding S2b through a scheduled event slightly HURTS (event gap-risk ≥ crush-benefit), fails the null,
reverses OOS, and dies under cost. This is a 5th failure of vol/IV-conditioned entry timing in this project.
Consistent with the whole body of work: the edge is SPY-Monday-specific and doesn't extend to event-timing.

Sample is small (72 spanned trades; 8 FOMC + 11 CPI events over 1 year) and CPI dates carry mild
uncertainty — but date noise would only blur a real edge, and there is none to blur; the point estimate is
already negative. Combined with the other-day scan, both remaining "other-day" hunts (turn-of-month not run;
post-vol-event here) are now closed on the negative side. **SPY-Monday remains the only robust edge; deploy
more capital by scaling Monday size, not by adding days or event-timing.**
