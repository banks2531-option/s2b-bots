# Systematic Futures Edges — Exhaustive Literature Review

Date: 2026-07-01 · Method: 5 parallel research agents across academic factor literature, CTA/managed-
futures practitioner research, commodity-structural (seasonality/term-structure/COT), retail
micro-futures deployability, and open-source/forums. Same standard as the options review: an "edge"
must plausibly survive a chronological tune/test split, beat permutation/shuffled-label nulls, and
survive a 2× transaction-cost stress. Assessed for our stack: free daily continuous-futures/ETF data,
Databento for intraday, a SMALL retail account, micro futures.

## The unanimous #1 answer: SLOW TREND + CARRY, combined
All five streams converged: the one robustly-evidenced, cost-aware, twice-replicated systematic futures
edge is **slow trend-following + carry, combined (~60/40)** — **Sharpe ~0.75, and it dominates either
alone** (Carver's pysystemtrade; independently reproduced by QuantConnect research #16001 at Sharpe
0.749). Carry is the genuine NEW factor vs our prior work: Carver's highest single-rule Sharpes are
**carry ~0.90–0.95**, above every trend variant. Academic backing is deep — Koijen-Moskowitz-Pedersen-
Vrugt "Carry" (JFE 2018); Erb-Harvey (FAJ 2006) show roll return explains commodity cross-sectional
returns with **R²=91.6%, t=10.97** (theory of storage: backwardation = scarcity premium paid to the
long).

**Two hard caveats that decide everything for us:**
1. **The validated edges are CROSS-SECTIONAL** — they need a *basket* of ~10–25 markets to diversify
   idiosyncratic blowups. Standalone single-market carry Sharpe is ~0.3–0.7 with **−78% drawdowns**,
   and decayed post-2004 (index-investing financialization compressed roll premia).
2. **Carry's tail is the OPPOSITE sign of trend** — it loads on recession/liquidity/vol risk and
   *crashes in 2008* exactly when trend pays. So carry diversifies **calm/ranging** periods (where
   trend whipsaws), NOT crises. Trend remains the crisis-alpha engine we already validated.

## Only the SLOW versions work; fast trades die on costs
Carver's per-rule Sharpes (50yr, 100+ instruments, after costs) are decisive:
- Trend (EWMAC): assettrend2 **−0.94**, trend16–64 **+0.62 to +0.70** — only ≥16-day survives.
- Breakout: breakout10 **−1.19**, breakout80–320 **+0.77 to +0.79** — slow only.
- Rel-momentum: relmomentum10 **−1.86** — fast momentum is destroyed by costs.
- Carry (carry10–125): **0.90–0.95**; Skew (AFTS Strat 24): Carver rates "brilliant," genuine
  diversifier, exotic to build.
His transferable cost gate: **exclude any rule costing >0.15 SR-units per instrument.** And the plain
TSMOM "predictability" claim is contested (Huang-Li-Wang-Zhou, JFE 2020: 47/55 assets individually
insignificant; incremental edge over a static long tilt unproven) — trade slow trend, don't overpay for
the "timing" story.

## What the evidence KILLS (documented losers — do not chase)
- **Fast intraday trend / ORB / VWAP-reversion:** a 2026 falsification study on MNQ found **negative
  net results across all opening-range configs**; the "74% win-rate NQ" backtests are the overfit
  counterexamples.
- **Overnight drift:** REAL (S&P +47% overnight vs +30% intraday; NY Fed 2–3am ET drift) but **dies on
  costs** — 10bp commissions = ~100%/yr drag; not retail-tradeable. Real anomaly, off-limits.
- **Naked short-vol / VIX-contango harvest:** XIV **−96% in a day** (Volmageddon, Feb 2018), a
  structural blowup. Only DEFINED-risk short-vol survives (= our S2b).
- **COT / "follow the commercials" directional timing:** folklore — Granger-causality tests find trader
  positions do NOT forecast returns (they're trend-followers reacting to price). Only the *cross-
  sectional hedging-pressure factor* (Basu-Miffre) has weak support, and it just overlaps carry.
- **Natural-gas calendar seasonality:** DECAYED post-shale — EIA & RBN document winter's consumption
  share falling and seasonality flattening; a 1990–2008 NG seasonal FAILS out-of-sample in the 2010s.
- **Precise "buy Sept 2 / sell Oct 20" seasonal rules:** data-mined; MRCI's "80% reliable" is an
  in-sample hit rate. (RB gasoline summer-driving and grain harvest seasonality are the only
  fundamentally-defensible ones, and need strict OOS.)
- **Long-only commodity buy-and-hold:** negative-carry tax — **UNG −89%/10yr, USO −14.6%/yr, BOIL
  −99.98%** from contango roll bleed.
- **ARP bank "style-premia" bundles:** ~29% dead by 2022 (value winter + 2019 momentum crash + forced
  deleveraging into Mar-2020). Real in-sample, crowded/decayed live.
- **"Diversified" multi-strategy CTAs underperformed pure trend** in real crises (Kaminski) — added
  premia diluted crisis alpha.

## The retail reality (decisive for THIS project)
- **Tradier does NOT trade futures.** Deploying any of this needs a NEW broker (Tastytrade, IBKR,
  Tradovate, NinjaTrader, AMP) — outside our current live stack.
- **Micro futures democratize ACCESS, not EDGE.** MES/MNQ/M2K/MYM (equity), MCL/MGC (commodity), micro
  yields/FX let a small account size risk sanely, but the validated edges are cross-sectional and a
  4–8-micro book **cannot build a real commodity cross-section** — so they collapse to weaker
  single-name/time-series versions. Carver's own system warns **sub-$50k accounts** suffer contract-
  lumpiness (discussion #1422).
- **Base rates are brutal:** Brazil index-futures study — of persistent (>300-day) day-traders, **97%
  lose**, 1.1% beat minimum wage. Barber-Odean: <1% of day-traders reliably profit. **Prop firms are a
  fee business** (~7% ever get a payout).
- **#1 futures-specific data trap:** back-adjustment ("Panama") trend bias — additive back-adjust
  injects drift and trend rules fire on the artifact. Mandatory guardrail: run every rule on
  proportionally- vs additively-adjusted series; if it only works on the back-adjusted one, it's the
  artifact, not an edge.

## Testable candidates for our stack (ranked)
1. **★ Time-series carry / roll-yield on micro crude (MCL)** — the one genuinely NEW single-market test.
   Long when own curve backwardated, flat/short in contango. FREE-ish to test (needs front + deferred
   series). Best mechanism (inventory→basis, the factor that replicated OOS). Honest prior: weak,
   undiversified vs the cross-sectional version; **gold (MGC) is near-permanent contango → carry dead,
   skip.**
2. **Slow-trend + carry 2-sleeve** (the ~0.75 crown jewel) — but only meaningful as a *basket*; a
   diversified micro book needs a futures broker + more capital than the live account has.
3. **Defined-risk index VRP** — literally our already-validated S2b; the futures literature independently
   corroborates it as the highest-Sharpe *accessible* edge. Never naked.
4. **Slow EWMAC trend alone (16–64d) / slow breakout (80–320d)** — simplest robust building blocks,
   good OOS first targets, reuse our trend machinery.

## Open-source tooling worth adapting
- **pysystemtrade (Rob Carver)** — THE reference managed-futures system: trend+carry, vol-targeting,
  forecast scaling/capping/combination, the 0.15-SR cost gate, dynamic optimization for small accounts.
  Run live by the author. `AFTS` book catalogs ~30 ranked rules.
- **QuantConnect #16001** — clean independent carry+trend replication (Sharpe 0.749) with honest caveats.
- **arbitragelab futures-rollover** / QuantStart — correct *proportional* continuous-contract stitching.
- **CFTC COT** (free) — only for the cross-sectional hedging-pressure factor, not directional timing.

## Verdict
Consistent with — and an extension of — the options review. The one robustly-validated systematic
futures edge is **slow trend + carry (~0.75 Sharpe, twice-replicated)**. But **trend is the same
crisis-alpha engine we already deployed defensively**, and **carry — the genuine new factor — is
cross-sectional (needs a basket) with a tail opposite to trend, so it diversifies calm periods, not
crashes.** For our actual constraints (tiny account, Tradier = no futures, no real cross-section
possible, retail intraday futures a documented graveyard), **futures offer no NEW deployable edge at
this scale.** The single new thing worth prototyping is **time-series roll-yield carry on MCL**; the
validated basket edge is parked-for-scale, exactly like the 5-ETF trend sleeve — it becomes real only
with a larger, multi-instrument, futures-enabled account.
