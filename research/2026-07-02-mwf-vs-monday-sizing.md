# Drop-Tue/Thu (MWF) vs All-Days vs Monday-Only — Compounding Account Sim

Date: 2026-07-02 · Script: `research/throttle/mwf_equity.py`
Tests the data-supported lever from the throttle study (per-weekday PF: Mon 1.86, Tue 0.80, Thu 0.71) as a
REAL compounding account at $5k and $20k. Ledger = the actual all-days S2b trades (`trades_panel_daily.csv`,
per-contract P&L, 2025-03..2026-02) + 2x-slippage twin. Sizing/concurrency mirror Bot B: 10%/trade (min 1
contract — the bot's floor), max 5 concurrent, open risk ≤50% of equity; unfittable entries skipped;
event-driven; balance compounds; qty scales P&L linearly (2x ledger stresses fills).

## Results
**$5,000 start — BASE slippage**
| strategy | final | return | maxDD | PF | took/skip |
|---|---|---|---|---|---|
| all-days | $2,589 | **−48%** | −64% | 0.83 | 150/96 |
| **MWF (Mon/Wed/Fri)** | **$8,360** | **+67%** | −19% | 1.36 | 146/0 |
| Monday-only | $6,709 | +34% | **−7%** | **1.86** | 47/0 |

**$20,000 start — BASE slippage**
| strategy | final | return | maxDD | PF | took/skip |
|---|---|---|---|---|---|
| all-days | $16,120 | **−19%** | −38% | 0.90 | 240/6 |
| **MWF** | **$27,843** | **+39%** | −12% | 1.30 | 146/0 |
| Monday-only | $23,675 | +18% | **−4%** | **1.89** | 47/0 |

**2x slippage (the realistic-cost world)**
| strategy | $5k final / ret | $20k final / ret | PF |
|---|---|---|---|
| all-days | $1,476 / **−70%** | $10,428 / **−48%** | 0.66 |
| MWF | $4,313 / −14% | $18,902 / −5% | 0.93–0.95 |
| **Monday-only** | **$5,861 / +17%** | **$21,799 / +9%** | **1.40** |

## Findings
1. **All-days should be abandoned — it LOSES money at every balance and cost level** (−19% to −70%), with
   deep drawdowns (−38% to −70%). At $5k it also had to **skip 96–147 of 246 entries** (couldn't fit under
   the risk cap) — the $10-wing all-days book doesn't fit a small account (the C7 strategy/account-fit
   problem, live). Confirmed money-loser.
2. **Dropping Tue/Thu (MWF) is a decisive improvement over all-days** — from −48%/−19% to **+67%/+39%** at
   base slippage, with roughly a third of the drawdown. The Tue/Thu removal alone flips a loser to a winner.
3. **But MWF is NOT clearly better than Monday-only — and is cost-fragile.** At base slippage MWF makes more
   *dollars* (more volume: 146 vs 47 trades), but at **lower PF (1.30–1.36 vs 1.86–1.89) and 2–3× the
   drawdown.** Under realistic **2x costs MWF decays to breakeven/negative (−14% to −5%)** while
   **Monday-only stays positive (+9% to +17%, PF 1.40) — the ONLY strategy that survives the cost stress.**
4. **Small-account leverage effect ($5k vs $20k):** returns are similar in % terms (edge is scale-invariant),
   but the **min-1-contract floor over-leverages the $5k account** (one $10-wing ≈ 15% of $5k vs ~4% of
   $20k), amplifying both return and drawdown (MWF $5k +67%/−19% vs $20k +39%/−12%). A $5k account really
   wants **narrower wings**, not $10 wings.

## Verdict
The data supports **dropping Tue/Thu**, but the more important conclusion is that **Monday-only is the
robust core**: highest PF (1.86–1.89), smallest drawdown (−4% to −7%), and the only variant that stays
positive through the 2× cost stress at both balances. **MWF is a reasonable "more-volume" variant only if
real costs are low; it is cost-fragile.** All-days is a confirmed loser and, on a small account, an
outright account-fit failure. Recommendation: bias the bots to **Monday-only** (or Mon + at most one strong
secondary day like Friday, PF 1.42), and for a small account narrow the wings before adding days.

Caveats: 1-year sample, no sustained bear (so this doesn't retire the 200d gate); Monday-only n=47 is small;
drawdowns are on the realized-equity curve (understate intraday open-position marks); qty scales P&L
linearly. Truth sits between the base and 2x brackets, closer to 2x for a small retail account's real fills.
