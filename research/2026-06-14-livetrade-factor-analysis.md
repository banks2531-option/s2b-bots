# Live Bot-B Trade Dissection — Factor Quality & Pattern Analysis (Parts 3–4)

Date: 2026-06-14 · Inputs: 81 broker-truth spreads (`droplet_data/tradier_truth_outcomes.csv`, Feb 27 – May 6 2026), scored against `docs/options-trade-factor-reference.md`. Pipeline: `research/livetrade/score_factors.py` → `livetrade_factors.csv`, `livetrade_scored.csv`.

## Headline: the bot didn't have an *entry* problem — it had a *payoff-asymmetry* problem.

| metric | value |
|---|---|
| Win rate | **55.6%** (45 / 81) — *fine* |
| Avg winner | **+$63** |
| Avg loser | **−$118** |
| Loser/winner ratio | **1.87×** |
| Net | **−$1,405** |
| Worst 5 losses | −$333, −$315, −$279, −$277, −$253 |
| Best 5 wins | +$300, +$182, +$158, +$149, +$149 |

A 56% win rate loses money when losers are ~1.9× winners. **No entry filter fixes that** — it's a trade-construction and risk-management failure. The bot took 50% take-profits on winners (capping them small) while letting losers run toward max loss (no effective stop). That single asymmetry is the dominant cause of the −$1,405.

## ADDENDUM — Real entry-factor data (pulled from droplet `ml_trade_data_live_b.csv` + `shadow_rejections_botb.jsonl`)

The bot recorded its *actual* entry-time factors. This replaces the modeled values above with measured ones, and it is decisive for your "inject intelligence" thesis: **bot B already HAD intelligence — an ML model, conviction/signal scores, whale-premium flow, a probability-of-profit model, an expected-value gate, and adaptive selectivity. That intelligence was neutral-to-anti-predictive.**

Real factors, winners vs losers (62 OPENs matched to broker-truth):

| factor (measured) | winners | losers | read |
|---|---|---|---|
| `entry_signal_strength` | 85.8 | **87.1** | losers scored *higher* — the bot's signal was **anti-predictive** |
| `entry_conviction` | 95.8 | 95.5 | pinned near max — no information |
| **`entry_whale_premium`** | **$13.5M** | **$41.2M** | **losers had 3× more whale flow** — the more "whales," the worse the trade |
| `entry_prob_profit` | 0.661 | 0.619 | barely separates |
| `entry_delta` | −0.01 | +0.02 | negligible |

**The intelligence was the problem, not the cure.** The single strongest relationship is that **bigger whale-premium flow predicted *worse* outcomes** — the 6th independent confirmation that flow isn't a directional edge here. The bot's own conviction/signal scores carried no winner-vs-loser information.

**Probability model was overconfident:** claimed mean P(profit) **64.4%**, realized **58.1%** — a ~6-point calibration gap, so the EV gate was working off inflated odds.

**The asymmetry, now mechanically proven by exit reasons:** winners closed at "**Take profit: 50% of credit**" / "Scaled profit Tier 4" (capped small); losers closed via "**Credit 1-DTE exit: 1 DTE with −377.8%**", "−93.5%", "−85.8%", "−81.1%". Stop-loss prices were set on all 114 opens but `scaled_stop_level` was set on **0** — **the stops never fired; losers ran to expiry.** Winners +50%, losers −80% to −378%. That is the entire −$1,405, in one mechanism.

**Shadow rejections:** all 353 declined candidates were rejected for one reason — `phase_e_ev` (negative expected value, mean −$56). So the bot was *gating hard on EV* yet still lost — because the POP/EV model feeding the gate was optimistic. This is the "Phase E EV gate sealed the funnel; bot silent 5 weeks" pathology from the rebuild diagnosis, seen from the inside.

**Implication for the new bot:** more prediction-intelligence of this kind won't help — it's already been tried and was anti-predictive. The fixes are **mechanical**: (1) symmetric risk — a stop that actually fires so losers ≈ winners; (2) the right *vehicle* (index VRP, not single-name directional); (3) a *calibrated* probability model. "Intelligence" should mean disciplined gating + risk management + calibration, not a stronger directional signal.

---

## What the factors say (winners vs losers, per the reference rubric)

Discriminating power = (mean factor score among winners) − (among losers):

| factor | winners | losers | gap | read |
|---|---|---|---|---|
| **AB — trend alignment** | +0.29 | +0.06 | **+0.23** | strongest signal: winners were with the tape |
| F1 — structure (debit-directional flag) | −0.02 | −0.17 | +0.14 | directional debit spreads hurt |
| F3 — risk/reward | +0.53 | +0.42 | +0.12 | minor |
| I3 — concurrency | +0.47 | +0.36 | +0.11 | minor |
| F2 — credit/width | +0.51 | +0.42 | +0.09 | minor |
| E6 — cushion (ATR) | −0.78 | −0.78 | 0.00 | **both terrible** — see below |
| D1 — earnings | +1.00 | +1.00 | 0.00 | bot already avoided earnings (not the problem) |
| B2 — RSI | +0.67 | +0.86 | −0.19 | noise |

**The structural indictment (E6):** average OTM cushion was **0.73 ATR (winners) vs 0.38 ATR (losers)** — *both under 1 ATR*. The bot systematically sold strikes too close to spot; only **10 of 81** trades had ≥1 ATR of breathing room. Thin cushion is *why* losers hit big — the short strike was almost always within one day's range of being tested.

## Candidate filters (IN-SAMPLE, n=81 — diagnostic only, NOT a validated rule)

| rule | n | win% | net | avg P&L |
|---|---|---|---|---|
| all | 81 | 56% | −$1,405 | −$17 |
| credit-only (drop directional debits) | 74 | 59% | −$1,169 | −$16 |
| **trend-aligned** | 48 | 60% | **+$45** | +$1 |
| cushion ≥ 1.0 ATR | 10 | 80% | +$122 | +$12 |
| credit + cushion≥1.0 + aligned | 8 | 75% | −$17 | −$2 |

**Trend alignment alone flips the book from −$1,405 to roughly breakeven** (n=48 — the only filter with both effect *and* enough sample to take semi-seriously). Cushion ≥1 ATR looks great (80% win) but n=10, and stacking filters collapses n to 4–8 — that's the overfitting cliff, not a discovery.

## The candidate "formula" (a hypothesis, with the honesty label attached)

From the dissection, a defensible scoring rule for the *next* bot — to be **validated out-of-sample before any capital**, per the project's C4 discipline (n=81 is a net-losing sample; in-sample filters that "work" mostly do so by shrinking n):

```
ENTER a defined-risk CREDIT spread only if ALL of:
  1. Trend-aligned        (bull-put: underlying > 20d SMA;  bear-call: < 20d SMA)   [strongest evidence]
  2. Cushion ≥ 1.0 ATR    (short strike ≥ 1 ATR OTM)                                [strong but n=10]
  3. Credit/width 0.25–0.40                                                         [payoff quality]
  4. Not a directional DEBIT spread (drop bull_call/bear_put)                       [1/6 and 0/1 live]
  5. No earnings before expiry; place in elevated-IV regime (VIX %ile ≥ 0.3)
AND — the part that actually matters most —
  6. A REAL loss cap: stop at ~1.5–2× credit (or short-strike-touched), so a loser
     ≈ a winner in magnitude. Fixing rule 6 matters more than rules 1–5 combined.
```

## Why this connects to the bigger picture (parts 1 + 2 + Stage-1)

Bot B was trading **single-name directional credit spreads across 27 scattered tickers** (AAPL, NVDA, COIN, XOM, UNH, …). The part-1 strategy research concludes the durable, capturable options edge is **index VRP harvested with defined-risk spreads** — *not* single-name directional premium selling, where idiosyncratic gap risk (the −$333/−$315 losers) overwhelms the modest premium. So the dissection and the literature agree: **the vehicle was wrong.** Bot B was collecting small premium on names with fat idiosyncratic tails and no loss cap — the worst combination. This is also consistent with the Stage-1 result (flow-direction doesn't predict; the edge isn't in picking direction on single names).

## Honest limitations
- **n = 81, net-negative, single ~10-week regime.** Every filter above is a *hypothesis*. The cushion and combined filters are small-n (4–10) and must not be believed without OOS validation on fresh trades.
- **Several factors were modeled/approximate** (greeks, IV-rank, flow) or unavailable locally (the bot's real entry-signal values live on the droplet). IV-rank/skew (C2/C4) — among the highest-evidence edge levers — could not be scored from local data; a droplet pull of bot-B's entry logs would materially upgrade this analysis.
- **Direction of causality:** "trend alignment helped" on 48 trades in one trending window may not generalize to a chop regime.

## Recommendations for the new bot
1. **Switch the vehicle to index VRP** (XSP/SPY/SPX-mini defined-risk spreads), regime-gated on IV-rank + VIX term structure — the one edge with both external evidence and internal OOS support (= the S2b family).
2. **Fix payoff symmetry first:** enforce a real stop so losers ≈ winners. This alone would have roughly halved the bot-B loss.
3. **Strike discipline:** ≥1 ATR cushion minimum; never sell strikes inside one day's range.
4. **Drop single-name directional debit spreads** entirely.
5. **Then** layer the trend/IV conditioners — and validate the composite rule OOS before funding it.
6. **Optional upgrade:** authorize a read-only droplet pull of bot-B's entry logs to score the IV-rank/greeks/flow factors with *real* values, not models.
