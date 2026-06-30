# LLM-Gated Trade Selection + Long-Convexity Edge — Backtest & OOS Verdict

Date: 2026-06-30 · Inputs: 81 broker-truth flow trades (`research/livetrade/livetrade_factors.csv`);
252 earnings straddles built from yfinance earnings dates + Polygon OPRA option bars
(`research/convexity/build_dataset.py`, `events.csv`). Judges: 81 + 98 isolated Claude
agents via Workflow. Scoring/OOS: `research/convexity/oos_validate.py`, `oos_pit.py`.

## Question
Two theses the user wanted settled empirically: (1) can **Claude, as the live decision-maker**,
select winning trades from full analytics where mechanical/statistical methods couldn't? (2) is
there a **high-upside long-convexity edge** (buy options for the big move) that Claude can capture?

## Method (rigor rails)
- Each trade judged by a **fresh, isolated Claude agent** seeing only **point-in-time** factors,
  never the outcome. Decisions: EXECUTE/PASS. Post-cutoff dates → no model memorization.
- Every result graded against a **random-selection null** (10k shuffles → p-value), the test that
  exposed the meta-selector (p=0.20) as information-free.
- Convexity edge taken through full **C4/C5 OOS**: chronological H1-tune / H2-test, 2× cost stress,
  per-year decay, and a **look-ahead audit** of every feature.

## Result 1 — LLM gate on credit spreads: NO edge
81 flow trades (net −$1,405). Claude EXECUTEd 4/81; gated net −$13 vs random-4 mean −$70,
**p=0.42**. Its selection is indistinguishable from chance; the only "benefit" was mass-rejection
(trading less on a −EV book). Confirms the credit-spread edge is **structural, not selective**.

## Result 2 — LLM gate on long earnings straddles: found the factor, then degraded it
Buying every straddle bleeds (−$11k in-sample). Claude's selection **was** significant
(**p=0.029**, +$7,150 gross) — convexity is genuinely a selection problem. But stress tests showed:
- The edge is **one mechanical factor**: implied move < the name's historical realized move.
- A pure rule (ratio<0.9) **beat Claude** (+$10,457 vs +$7,150); Claude's extra "judgment" trades
  lost money.
- Under realistic 10–15% round-trip cost, **Claude-gated goes negative** (over-trades), while the
  tight mechanical rule survived in-sample.
**Lesson: Claude is a good factor-finder, a worse executor than the rule.**

## Result 3 — the mechanical rule OOS: VALIDATED, then KILLED by a look-ahead audit
Expanded to **252 events / 39 names**. The OOS harness initially passed emphatically:

| Underpriced-straddle rule, H2 (OOS) | n | Net @10% cost | 2× cost | Null p |
|---|---|---|---|---|
| **hist_mean from full earnings history (look-ahead)** | 23 | **+$16,090** (PF 2.82) | +$10,776 | **0.002** |
| **hist_mean point-in-time (prior earnings only)** | 71 | **−$2,265** (PF 0.93) | −$15,080 | **0.138** |

The "historical" move feature was computed from the ticker's **entire** earnings history, including
events **after** each trade. Recomputing it **point-in-time** (only prior earnings) — a one-line fix
to the trader's real information set — **collapsed the edge**: no longer beats the null, negative
net of cost. The PF 2.82 was the bias, not an edge. Classic PF 5→0.5 collapse, caught.

## Verdict
- **An LLM as trade-picker adds no edge** — random on credit spreads; on convexity it found the
  right factor but its discretion hurt and died to costs.
- **No high-upside convexity edge survives look-ahead-free OOS validation.** The market prices the
  upside fairly, as the structural (VRP) argument predicts.
- **The only validated edge remains mechanical S2b** (sell weekend index VRP). Capital belongs there,
  sized with discipline — not behind an LLM gate or a long-convexity play.

## Why it's recorded
So the convexity/LLM-gate path is never re-litigated from scratch. The reusable asset is the
**method**: isolated point-in-time judges + random null + chronological OOS + a mandatory look-ahead
audit of every feature. The look-ahead collapse here is the canonical cautionary example.
