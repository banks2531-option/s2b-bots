# Trend-Following: the one validated edge (and the Phase 1 guard built from it)

Date: 2026-06-30 · Script: `research/trend/trend_test.py` (S&P 500 ^GSPC, 1950–2026, ~19k days).

## Context
After disproving LLM-gated selection, long-convexity, and (crash-tested via Databento) the
downside-skew harvest — all of which fail — we tested the one family the 2022-winners actually used:
**trend-following / time-series momentum** (price vs its 200-day MA). It is the first strategy this
project has found that passes every rigor test.

## Result (200d MA signal, vs buy-and-hold, 76 years)
| Strategy | CAGR | Max drawdown | Sharpe |
|---|---|---|---|
| Buy & hold | 8.2% | **−56.8%** | 0.58 |
| **Trend long/flat** | 7.0% | **−29.6%** | **0.71** |
| Trend long/short | 4.3% | −53.0% | 0.35 |

- **Beats random timing at p=0.001** (Sharpe 0.71 vs 0.49). **Robust** across MA 100/150/200/250
  (Sharpe 0.61–0.71) — not curve-fit.
- The edge is **risk reduction, not more return**: slightly lower CAGR, but ~half the drawdown and a
  better Sharpe. It works by **getting OUT of crashes** (2008: long/flat 0% vs buy-hold −38.5%).

## Honest nuance
- The **short side is too whippy to stand alone** (long/short underperforms over the full sample —
  it wins in clean bears like 2008 +37%, but bleeds in chop and V-recoveries like 2020 −16%).
- **2022 specifically was a HARD year for simple S&P trend** (long/flat −15.9% vs buy-hold −19.4%) —
  it was a choppy, grinding bear with violent failed rallies that **whipsawed** the 200d signal. The
  "CTAs crushed 2022" story is **multi-asset** (short bonds, long commodities), not S&P-timing.
- So the durable, validated form is **DEFENSIVE long/flat** (avoid crashes), not offensive shorting.

## What was built from it (Phase 1, deployed 2026-06-30)
A **trend gate** in `bot/regime/`: `trend_regime(bars, ma=200)` → risk_on/risk_off/unknown, surfaced
on `RegimeState.trend_regime`. `orchestrator.tick()` computes the regime up front; `run_entry_cycle`
**pauses NEW put-spread entries when SPY < its 200d MA** (risk_off). Fail-safe: acts only on a
positive risk_off; unknown/fault → normal trading. `trend_gate_enabled=True` on all 3 bots (live +
sandbox). Management/stops unchanged (it gates entries, doesn't yet de-gross existing positions).
Verified live: SPY 746.77 > 200d MA 691.39 → risk_on → no current change; trips if SPY falls ~7%+.

## Why it matters
This is the data-backed answer to "what do we do in a sell-off": **not a clever options trade — stop
selling premium into the downtrend.** It's the only edge that survived, and it's defensive. A future
extension is de-grossing existing positions on risk_off (more aggressive; not yet validated/built).
