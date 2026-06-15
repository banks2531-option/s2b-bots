# March–April Live Run-Up Dissection (bot B)

Date: 2026-06-15 · Source: `droplet_data/tradier_truth_outcomes.csv` (81 broker-truth spreads) + `research/livetrade/droplet/ml_trade_data_live_b.csv` (real entry factors).

## Question
The user recalled being "up over $1,000 in very little time with very little balance" in Mar–Apr and asked to examine the profitable trades and replicate them with risk mitigation.

## What actually happened
- **Broker-truth cumulative P&L peaked at +$772 on 2026-04-01** (after 41 trades), then fully reversed to **−$1,405 by 05-06**. (The bot's *self-reported* balance glitched to $72,254 — accounting drift, the documented drift-alarm; broker truth is the +$772.) On a ~$2k balance, +$772 ≈ **+38% in ~5 weeks** — the real run.

| | WIN run (2/27→4/1) | LOSE run (4/1→5/6) |
|---|---|---|
| net | **+$772** | **−$2,177** |
| win rate | 66% | 45% |
| avg win / loss | +$82 / −$102 | +$36 / −$128 |
| **SPY move** | **−4.5%** | **+12.0%** |
| **IWM move** | **−4.5%** | **+14.9%** |
| VIX | +23.6% | −29.1% |

## The finding: it was a directional bet that paid, not a repeatable edge
- The **5 biggest winners were IWM/QQQ/AAPL bear-call spreads** — *bearish* positions (short calls above market). They won because the market **fell 4.5%** in that window.
- When the market **rallied +12%** (Apr–May), the *identical* short-biased book got run over: the 2 worst trades were an **AAPL bear-call (−$333)** and an **IWM bear-call (−$315)** — same trade type that won in March, now breached.
- So the profit was **theta + being short into a dip**, not trade-selection skill. Direction is the thing this project has disproven 7 ways; the April reversal is what happens when the unhedged directional bet is wrong.

## Replicable method (engine yes, luck no)
You cannot replicate "the market fell while I was short." The repeatable part is **defined-risk index credit-spread premium harvesting** (= S2b). The part that caused the giveback was **no risk control at the regime flip**. Risk-mitigated version:
1. Index-only, defined-risk credit spreads (they mostly were — keep).
2. **A stop that actually fires** (the −$333/−$315 losers had none).
3. **≥1 ATR cushion** on short strikes.
4. **Cut size when VIX is spiking** (the win-run ran into VIX +23% — max reward *and* max reversal risk).
5. Stay delta-balanced (the March book was ~20 bear-call + 20 bull-put ≈ index condors → PF ~1.11, a harvest not a miracle).
6. **Drop directional debit spreads** (bull-calls went 1-for-6).

→ This is **S2b done right**: keeps the March upside, survives the April reversal. Codified in `docs/specs/2026-06-15-s2b-execution-bot-design.md`.
