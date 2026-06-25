# Daily Strategy-Selector Test — does "pick the best-looking strategy each day" work?

Date: 2026-06-25 · Code: `research/meta/selector_test.py`. Tests the meta-bot idea: run K strategies, each day pick the one that looks most lucrative (trailing performance), trade it. 6 candidate flow strategies (sweep premium tiers × GEX regimes × floor prints), 69 trading days, vol-normalized daily returns, walk-forward (no lookahead).

## Verdict: NO — the selector is information-free. Picking the best-recent strategy is no better than chance.

## The trap, and the instrument that exposes it

**Naive read (what fools you):** the selector cumulatively returned **8.79** vs equal-weight's **5.24** → "the selector beats holding everything — it works!"

**The kill — a 300× time-shuffle null** (destroy all day-to-day persistence, then re-run the same selector):
| | real data | shuffled (no persistence) |
|---|---|---|
| selector cumret | **8.79** | mean **3.57**, 95th pct **14.22** |
| **p(shuffled ≥ real)** | — | **0.200** |

The real selector (8.79) sits **right inside** the distribution of a no-information selector run on persistence-destroyed data (20% of shuffles do as well or better; the 95th percentile is 14.2, far above 8.8). **The trailing-performance signal carries no predictive information.** The selector's apparent edge over equal-weight is **concentration/variance luck** in a benign in-sample window — not selection skill. (Its t-stat was 1.33 anyway — not significant.)

## Why this is the project's core failure mode, mechanized
Picking the max of K noisy estimates each day **concentrates** overfitting rather than reducing it — you systematically select whichever strategy got luckiest, and luck reverts. The 300-shuffle p = 0.20 is the formal proof: there is no day-to-day performance persistence to exploit.

## And even a *perfect* selector wouldn't help
The perfect-foresight ceiling (pick the actual best strategy each day with future info) returned **61.9** (PF 205) — huge theoretical room, of which the realistic selector captured ~none. But that ceiling is on **gross underlying signed returns of flow signals we already proved are OOS-edgeless** (Stage-1: H2 ≈ 0, p = 0.49) and **net-negative after option costs**. There is nothing real to select even with perfect selection.

## Both flavors of the selection score are dead
- **Trailing performance** ("looks lucrative") → information-free (this test, p = 0.20).
- **Model EV / probability** (the other natural score) → already failed: Test A's multi-factor model hit OOS AUC 0.47; bot B's own probability model claimed 64% and delivered 58%.

## Implication
A daily strategy-selector does not manufacture edge from edgeless ingredients — it amplifies selection bias. The path to a real meta-bot is the reverse: build a **library of individually OOS-validated edges** first (we have one, S2b), then select among them on **causal regime preconditions** (IV-rank, term structure), never on recent performance. The live A/B on the droplet is step one of growing that library.
