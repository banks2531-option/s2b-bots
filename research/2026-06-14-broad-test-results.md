# Broad Multi-Factor Test — Results (entry formula + exit optimization)

Date: 2026-06-14 · Scope: the "broader test" — derive better **entry and exit** formulas from the full factor set across all cached historical data, OOS-validated vs the validated **S2b** baseline. Code: `research/broad/entry_analysis.py`, `simulations/exit_sweep_s2b.py`. Ran entirely on cached data (no API pulls).

## Verdict: neither lever beats S2b out of sample. The validated mechanical S2b entry+exit is confirmed robust and remains the deployment candidate.

This is the broad version you asked for, run with proper anti-overfit instruments. It is more thorough than Test A (added VIX term-structure, interpretable rule search, a permutation null, *and* the exit dimension Test A never touched). The answer is the same, now established more widely.

## 1. Entry — does any multi-factor formula generalize? NO.

246 mechanical SPY put-spread trades × 41 factors (the 39 from Test A + VIX **term-structure/contango**), target = pnl-per-risk, H1-tuned / H2-tested.

- **Interpretable single-factor rules look great but are search artifacts.** Best H2 rule: `vix_close ≥ 18.4` → 93.8% win, +0.107 H2 mean. But the **permutation null** (shuffle outcomes, rerun the *same* search) produces a "winner" that good or better **17.7% of the time (p = 0.177)**. So those rules are what 41 factors × thresholds manufacture by chance — not signal.
- **Full logistic OOS AUC = 0.482** (≤ chance), confirming Test A's 0.47–0.52.
- **Term-structure didn't rescue it** (89% of days were contango this window → barely selective).

Economically: "sell when IV is elevated" (VRP conditioning) is *directionally* sensible and showed mild H2 lift, but at this n it's inside the noise the search generates. Not a deployable formula.

## 2. Exit — can re-optimizing TP/stop beat S2b's 50%/2×? NO.

12-config grid (tp ∈ {0.35, 0.50, 0.65} × stop ∈ {1.0, 1.5, 2.0, 2.5}) on Monday-S2b, cache-only, H1-tuned / H2-tested, tail-guarded.

| config | H1 PF | H2 PF | H2 pnl | maxDD | note |
|---|---|---|---|---|---|
| **0.50 / 2.0 (baseline S2b)** | 1.84 | **1.97** | $1,655 | $963 | robust across halves |
| 0.50 / 1.5 (best on H1) | **2.00** | 1.62 | $1,331 | $1,160 | H1-best **underperforms** baseline OOS |
| 0.50 / 1.0 (tight stop) | 1.76 | **0.59** | −$1,707 | $2,347 | tighter stop = whipsaw, **catastrophic OOS** |
| 0.65 / 1.0 | 1.65 | 0.54 | −$2,130 | $2,872 | worst |
| 0.35 / {1.5,2.0,2.5} | 1.3–1.5 | 4.08 | $2,094 | ~$700 | great H2 but **bad H1** → not selectable |

- **The H1-tuned pick (stop 1.5) loses to the baseline OOS** (H2 PF 1.62 vs 1.97). The discipline correctly rejects it.
- **Tighter stops are actively harmful** — they whipsaw out of trades that recover (H2 PF collapses to 0.5–0.9). The intuitive "cut losers faster" fix *backfires* on a properly-structured index credit spread.
- **The S2b baseline is remarkably stable: H1 PF 1.84 / H2 PF 1.97** — consistency across halves is the *opposite* of overfit, and a strong endorsement of it as deployable.

## 3. The key reconciliation with the bot-B asymmetry finding

The bot-B dissection said "payoff asymmetry / stops that never fired sank it." That was true **for bot B** — single-name directional spreads with broken execution (`scaled_stop_level` set on 0/114). It is **not** a flaw in S2b: on the index structure, the stop already fires, maxDD is a modest $963, and there is no fat tail. So the asymmetry fix is **execution discipline (actually fire the stop) + the index vehicle**, both of which S2b already has in backtest — not a new exit formula.

## 4. What the broad test did NOT cover (honest scope)
- **Exotic exits** — trailing stops, partial profit-scaling, vol-target exits — were not tested; a possible (low-prior) future item.
- **n is small** — H2 ≈ 23 Monday trades; a *positive* would need discounting, but the *negative* (H1-best underperforms, tighter stops backfire) is consistent and robust.
- Entry rule search was single/pair-factor; deep interactions weren't exhausted, but the full ML model (Test A + this AUC 0.48) already covers interactions and found nothing.

## 5. Recommendation
The rigorous broad test confirms: **the edge is the validated mechanical S2b (index VRP, Monday, 30–40Δ put spread, TP 50% / stop 2× / 1-DTE exit), and it is not improved by multi-factor entry gating or exit re-optimization.** The path to "far better results" is **not** a smarter formula — it is:
1. **Deploy S2b** at sane sizing (the scaling study's ~$310–$3,674/yr range depending on sizing/compounding).
2. **Execution discipline** — the live bot must actually fire the stop (bot B's didn't).
3. **Vehicle** — keep it on liquid index products (SPY/XSP), never single-name directional.
4. If still hungry for a new edge: pre-register a *non-directional vol* thesis or exotic-exit study with eyes open that priors are weak — but stop conditioning on flow/direction (now disproven 6 ways) and stop expecting a multi-factor entry formula to generalize (disproven twice, rigorously).

### Artifacts
- `research/broad/entry_analysis.py` → `entry_rules.csv` (rule search + permutation null)
- `simulations/exit_sweep_s2b.py` → `exit_sweep_results.json` (12-config H1/H2 exit grid)
