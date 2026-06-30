# Multi-Asset Trend-Following Test — Result

Date: 2026-06-30 · Script: `research/trend/multiasset_test.py`
Basket: SPY, TLT (bonds), DBC (commodities), GLD (gold), UUP (USD) · Window 2007-03 → 2026-06 (UUP
inception binds; covers 2008, 2018, 2020, 2022). Signal: per-asset sign(price − 200d MA), acted next
day, inverse-vol risk-scaled, portfolio vol-targeted to 10%/yr. Long/SHORT per asset (the bond-short
is what made 2022). Discipline: H1/H2 split, per-crisis-year, random-sign null (2000 draws), MA
robustness, 2× cost stress, + a portfolio-overlay test.

## Headline (full window, after 5bps/switch costs)
| Book | CAGR | maxDD | Sharpe |
|---|---|---|---|
| Buy & hold SPY | 9.0% | −56.5% | 0.54 |
| Equity-only trend L/S | 2.9% | −25.8% | 0.31 |
| **Multi-asset trend L/S** | **−0.7%** | −52.2% | **0.03** |

**Standalone, multi-asset trend is NOT a validated edge on this window:** Sharpe 0.03, negative CAGR,
**fails the null (p=0.448)**, and **fails the 2× cost stress** (Sharpe 0.03 → −0.12). The MA-robustness
sweep is uniformly near-zero (MA100–250 Sharpe 0.03–0.12). It bleeds in the long calm/bull stretches
(2017 −4.4%, 2023 −16%) — the negative carry of insurance eats the crisis gains over a bull-dominated
19-year sample.

## But the 2022 delta is CONFIRMED — and the mechanism is exactly the literature's
| Year | buy-hold | equity-trend | multi-trend |
|---|---|---|---|
| 2008 | −38.3% | +14.7% | **+12.8%** |
| 2020 | +16.2% | +0.9% | **+16.7%** |
| **2022** | **−19.5%** | **−6.7%** | **+6.5%** |

Equity-only trend **lost −6.7% in 2022** (whipsawed by the choppy grind — same failure as the S&P-only
test). Multi-asset trend **made +6.5%**. 2022 per-asset attribution (risk units):
- **TLT short +1.73** (signal −0.99 = fully short bonds) — the largest leg
- **UUP long +1.15** (long dollar)
- DBC long +0.10 (commodities); SPY-short −0.84 (small drag); GLD −0.73 (chop)

→ **Short-bonds + long-dollar carried 2022**, exactly as Hurst-Ooi-Pedersen / AlphaSimplex describe.
Our equity-only test failed in 2022 because it lacked breadth and a short side — now demonstrated on
our own data, not just asserted from the literature.

## It's a HEDGE, not an alpha — the overlay test
corr(SPY, trend): full −0.08, **2008 −0.56, 2020 −0.21, 2022 −0.52** (negatively correlated in
crises). Overlaying a vol-targeted trend sleeve on buy-hold SPY:
| w_trend | CAGR | maxDD | Sharpe | 2022 ret |
|---|---|---|---|---|
| 0% | 9.0% | −56.5% | 0.54 | −19.5% |
| 10% | 8.4% | −53.1% | 0.54 | −16.8% |
| 20% | 7.6% | −51.0% | 0.54 | −14.2% |
| 30% | 6.8% | −49.1% | 0.54 | −11.5% |
| 50% | 5.0% | −45.8% | 0.47 | −6.2% |

The sleeve **monotonically cuts drawdown** (−56.5% → −45.8%) and **cuts the 2022 loss** (−19.5% →
−6.2%) — but it's **Sharpe-neutral** (flat 0.54, declining at high weight). It trades CAGR for
drawdown reduction at a roughly fair price. That is insurance priced fairly, **not free alpha** — fully
consistent with AQR "Put vs Trend" and the crisis-alpha literature on a single bull-dominated window.

## Verdict
The user's premise was right *and* the project's hard-won reality holds, simultaneously: a validated
crisis edge exists (**multi-asset trend, short-bonds + long-dollar — it demonstrably captured 2022**),
**but it is DEFENSIVE — a fairly-priced, negatively-correlated hedge, not a standalone money-maker.**
There is still no free profit-from-the-crash alpha. The honest sell-off answer remains: reduce risk /
cushion drawdown. Phase 1's trend gate already does the cheap version (stop selling into downtrends).

## Recommendation
1. **Do NOT add a 5-ETF trend sleeve to the live bots** — Sharpe-neutral, operationally heavy, and the
   $400 live account can't run a vol-targeted multi-asset book. It would add cost/complexity for no
   risk-adjusted gain at this size.
2. **The decision-relevant extension is Phase 1.5 de-gross** — when the gate reads risk_off, don't just
   pause new entries, *reduce existing* exposure. That is the cheap, account-appropriate way to harvest
   the same negative-crisis-correlation this test confirmed, without running the sleeve.
3. The 5-ETF overlay is genuinely useful **only for a much larger, multi-instrument account** (futures
   access). Park it as documented-and-validated-as-a-hedge for that future scale.
