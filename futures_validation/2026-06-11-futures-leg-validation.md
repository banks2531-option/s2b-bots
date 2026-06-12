# Futures-leg validation (mes_trend, design spec §4.2) — 2026-06-11 (FINAL)

Validation of the three candidate MES setups for the rebuild's futures leg,
run at the same rigor as the options shortlist validation
(`simulations/2026-06-11-shortlist-validation.md`): time-split halves,
PF ≥ 1.3 in BOTH halves at baseline costs to pass, 2× slippage stress,
per-setup attribution, monthly P&L — plus a Topstep-style $50k eval overlay
with rolling start dates.

> **Headline: ALL THREE SETUPS FAIL.** F1 (midpoint_reclaim family) has
> literally zero gross edge (PF 0.99 *before* any costs, n=321) — the legacy
> "+$171 profitable cohort" was a 3-trade fluke. F2 (ORB long-only in UP
> regime) has negative gross edge (0.86) — regime-gating does not rescue
> opening-range breakouts. F3 (trend-day follower) is the only one with a
> real gross edge (PF 1.19, +0.91 pts/trade) but it is smaller than
> realistic costs at 1-contract MES scale and is concentrated in one half.
> The eval overlay confirms what the PFs imply: ~0–6% eval pass rates and
> 27–84% bust rates at tradeable size. **The honest $/month number for this
> leg as specified is negative.** Do not proceed to Stage 2 paper with any
> of these three; the futures leg needs a different entry concept (or a
> rebuilt F3 with wider targets/cheaper execution) before it earns paper
> time. Zero dollars were spent producing this result.

---

## STEP 1 — Data inventory

| Location | What was found | Usable for this task |
|---|---|---|
| `E:\BanksBackup\trading-bot\backtests\cache\api_cache.db` (1.5 GB SQLite, read-only) | 446,115 cached entries: 440,783 polygon, 4,589 tradier, **500 `futures:*`** = **ES.c.0 and NQ.c.0 1-min OHLCV, 250 sessions each, 2025-03-03 → 2026-02-27**, full Globex days (~1,380 bars/day), fetched by the legacy `prefetch_futures.py` from Databento GLBX.MDP3 (continuous front-month, calendar roll, unadjusted) | **YES — primary dataset.** Already paid for by the legacy project; $0 new spend |
| `E:\BanksBackup\trading-bot\backtests\outputs\` | Futures backtest *artifacts* (trades/equity/summary for `futures_trend` PF 1.15, `futures_backtest` PF 0.64, prop_orb / prop_vwap_mr / prop_gap / prop_ib runs) — results, not bars | Reference only (legacy claims to re-derive) |
| `E:\BanksBackup\trading-bot\` (parquet/csv sweep) | No futures bar files outside the cache | No |
| `C:\Users\FixUser123\trading-bot-claude-backup\`, `C:\Users\FixUser123\go-trader\` | No futures bar data >100 KB | No |
| Droplet `/root/trading-bot-k/data/` (read-only) | Bot state only: `orb_live_daily_ranges.json`, positions JSON, P&L snapshots — **no bar history** | No |
| Droplet `/root/prop-futures/data/` | `prop_daily_ranges.json` (1.7 KB), positions JSON — no bars | No |
| Droplet `/root/trading-bot-daytrader/data/bars/` | QQQ/equity 1-min parquet (Apr 2026, ~1 month) — not futures | No |

**MES note (clearly labeled):** the bars are **ES** (E-mini). MES is the same
instrument at 1/10 multiplier — identical price, identical 0.25 tick. All
economics below are MES ($5/pt, $1.25/tick). This is a standard, sound
substitution, not a SPY-style proxy; the only caveat is MES book depth is
thinner than ES, which the 1-tick (+1 on stops) slippage model is meant to
cover at 1–5 lot size.

**Quality (verified before use):**
- 246 of 250 cached sessions have RTH (09:30–16:00 ET) bars; median 390/390.
- **12 sessions are chunk-truncated** (end 12:04 or 11:04 ET) by a prefetch
  bug (`end={last}T16:05:00` parsed as UTC at 20-day chunk boundaries);
  3 more are real half-days (Jul 3, Nov 28, Dec 24). All 15 **excluded from
  trading** → **231 clean full sessions** over exactly 12 months.
- 14 weekdays absent: 10 CME holidays + **the 4 quad-witching Fridays**
  (3/21, 6/20, 9/19, 12/19 — the legacy day list came from an SPX-flow CSV).
- Continuous-contract **roll basis jumps land on the Mondays after those
  missing expiry Fridays** (verified vs ^GSPC same-interval moves: +0.6% to
  +1.6% divergence on 3/24, 6/23, 9/22, 12/22). All strategies are
  intraday-flat so only F3's prior-day-range reference is affected; F3 skips
  those 4 sessions.
- Coverage is right at the ~12-month minimum: enough to run the validation,
  thin for a final word. H1/H2 split matches the options work
  (H1 ≤ 2025-08-28 < H2). Regime mix: 162 UP / 62 CHOP / 31 DOWN days
  (SPY 20/50 SMA stack, point-in-time from prior close, via free yfinance).

## STEP 2 — Harness and strategy implementations

Clean harness `futures_validation/sim_futures.py` (sim_mech conventions:
declared rules up front, summary JSON + trades CSV outputs, no parameter
sweeps for selection). Execution: signals on completed 5-min bars, **fills
simulated on 1-min bars**; market/stop fills 1-tick base slippage, stop-loss
fills +1 extra tick; TP is a limit requiring trade-through; same-bar SL+TP →
SL first (pessimistic); $1.24/side commission; lunch skip 11:30–13:30 ET (no
new entries); no entries after 15:30; EOD flatten 15:45; daily loss cap $150;
1 contract MES; one position at a time. Execution engine spot-verified by
hand against raw bars (entry/stop fills and P&L reconcile to the cent).

- **F1 midpoint family** — exact legacy semantics from
  `orb_live_runner.py` (note: legacy "midpoint" = running *session* high/low
  midpoint, not the OR midpoint): long when prev two 5-min closes < mid and
  current close > mid (regime ≠ DOWN); short mirror (regime ≠ UP); SL 8 /
  TP 12; 1-bar cooldown; ≤4 entries/day.
- **F2 ORB long-only, UP regime only** — 15-min opening range; resting
  stop-entry at OR-high + 1 tick from 09:45; SL 12 / TP 16; re-arm only after
  a 5-min close back inside the range; chop-day damage cap = 2 stop-outs/day;
  ≤3 entries/day.
- **F3 trend-day follower** — if the first hour closes beyond the prior full
  session's RTH range, enter with trend at 10:30; trailing stop {8, 12, 16}
  pts behind best close (all three reported, none selected post-hoc); no TP;
  EOD flatten; skips roll Mondays.

## STEP 3 — Results

All cells: n / WR / PF / P&L ($, 1 MES). Gate: PF ≥ 1.3 in BOTH halves at
baseline costs.

| strategy / scenario | FULL | H1 (incl. Apr crash) | H2 (calm grind) | maxDD |
|---|---|---|---|---|
| F1 midpoint family | 321 / 39% / 0.80 / −1,769 | 149 / 38% / 0.77 / −966 | 172 / 40% / 0.82 / −803 | 1,737 |
| F1 2× slip | 321 / — / 0.67 / −3,121 | 0.61 / −1,786 | 0.73 / −1,335 | 3,085 |
| F2 ORB long UP | 167 / 41% / 0.73 / −1,633 | 71 / 48% / 0.88 / −290 | 96 / 37% / 0.63 / −1,343 | 1,751 |
| F2 2× slip | 169 / — / 0.66 / −2,213 | 0.75 / −685 | 0.60 / −1,528 | 2,296 |
| F3 trail 8 | 94 / 32% / 0.63 / −802 | 0.34 / −915 | 1.14 / +113 | 954 |
| F3 trail 12 | 94 / 36% / 0.95 / −144 | 47 / 32% / 0.69 / −492 | 47 / 40% / 1.33 / +347 | 772 |
| F3 trail 12, 2× slip | 94 / — / 0.83 / −469 | 0.61 / −650 | 1.16 / +181 | 892 |
| F3 trail 16 | 94 / 37% / 0.87 / −452 | 0.76 / −464 | 1.01 / +12 | 1,066 |

**Not one cell of the pass condition is met.** Nothing reaches PF 1.3 in even
one half except F3-t12's H2 (1.33) — exactly one half, the calm one, with the
H1 column at 0.69.

### Edge-vs-cost decomposition (baseline, gross = before commission+slippage)

| strategy | gross pts/trade | gross PF | net PF | cost/trade | reading |
|---|---|---|---|---|---|
| F1 midpoint | −0.05 | 0.99 | 0.80 | ~$5.3 | **No edge exists.** Coin-flip before a single cent of cost |
| F2 ORB long UP | −0.91 | 0.86 | 0.73 | ~$5.3 | **Negative edge even gross**, even long-only in UP regime |
| F3 trend-day t12 | +0.91 | 1.19 | 0.95 | ~$6.1 | Small real gross edge; costs at 1-MES scale fully consume it |

### Attribution and monthly P&L

- F1: reclaim longs n=227 PF 0.76 (−$1,479); rejection shorts n=94 PF 0.88
  (−$289). Both directions lose; the regime alignment changes nothing because
  the entry itself carries no information. Exits: 192 stops / 116 TP / 13 EOD.
  Monthly: 4 green of 12 (best +$381 Oct; worst −$632 Dec).
- F2: 89 stops / 53 TP / 25 EOD. Green months: 3 of 10 traded (UP-regime days
  only began in May). **H2 — the friendliest possible tape, a 6-month UP
  grind — is its worst half (PF 0.63)**: breakouts in a quiet grind revert.
  The legacy live result (orb_break −$609, 46 trades) replicates faithfully.
- F3-t12: shorts PF 1.17 (+$156, n=36 — April crash trend days); longs
  PF 0.82 (−$300, n=58). 6 of 12 months green. 85 of 94 exits are
  trail-stops; the edge depends entirely on the few runners.
- Re-derivation of the archive's "futures_trend PF 1.15": that artifact was a
  different system (EMA 20/50 cross, SL 10/TP 25, 354 trades, 36.7% WR) whose
  profit was 85% short-side P&L from the crash months, with no half-split,
  no slippage stress, and stop-loss exits 49% of trades. The honest
  task-specified trend-day follower on the same data does not reproduce a
  tradeable net edge.

## STEP 3b — Prop-eval overlay (Topstep-style $50k, bot-side)

Rules simulated: trailing max DD $2,000 on **intraday** equity peaks (floor
locks at $50,000 once peak ≥ +$2,000), daily loss limit $1,000, profit target
+$3,000. Equity sampled every 1-min bar incl. open-position MTM. **Every one
of the 231 trading days is one rolling eval-attempt start.** 1 MES results
are the literal validated configuration; 3/5 MES scale P&L linearly (eval
accounts allow it; declared assumption).

| strategy @ size | PASS | BUST | censored (data end) | med. days to pass | med. days to bust |
|---|---|---|---|---|---|
| F1 @ 1 MES | 0% | 0% | 100% | — | — |
| F1 @ 3 MES | 0% | **83.5%** | 16.5% | — | 50 |
| F1 @ 5 MES | 6.1% | **84.4%** | 9.5% | 58 | 22 |
| F2 @ 1 MES | 0% | 0% | 100% | — | — |
| F2 @ 3 MES | 0% | **79.2%** | 20.8% | — | 37 |
| F2 @ 5 MES | 0% | **80.1%** | 19.9% | — | 24 |
| F3-t12 @ 1 MES | 0% | 0% | 100% | — | — |
| F3-t12 @ 3 MES | 0% | 17.3% | 82.7% | — | 64 |
| F3-t12 @ 5 MES | 0% | 26.8% | 73.2% | — | 36 |

Reading: at 1 MES the strategies are too small to ever resolve a $50k eval
(every attempt still unresolved after up to a year — meanwhile paying the
subscription every month). At sizes that could resolve it, the negative edge
resolves it the only way it can: F1/F2 bust 4-in-5 attempts; F1's 6.1%
"passes" at 5 MES are lucky starts in front of its one good month. F3 mostly
just bleeds slowly until the data ends.

## Verdicts

| strategy | verdict | basis |
|---|---|---|
| F1 midpoint_reclaim family | **FAIL** | Gross PF 0.99 over n=321 — the entry has no informational content; the legacy +$171 cohort was n=3. Both halves, both directions, both cost levels negative |
| F2 ORB long-only in UP regime | **FAIL** | Negative gross edge (0.86); worst in the calm UP half it was designed for (H2 PF 0.63). Confirms and extends the diagnosis: the legacy ORB's problem was not its shorts — the breakout itself loses |
| F3 trend-day follower | **FAIL (closest to life)** | Real gross edge (PF 1.19, +0.9 pts/trade, consistent at all 3 trail widths) but below costs at micro scale; net PF 0.95 full, halves 0.69/1.33 — fails the gate decisively. Honest re-derivation does not reproduce the archive's PF 1.15 |

## The $/month math (honest)

- **Measured expected income at the validated configuration: negative.**
  Best arm (F3-t12) = −$144/yr at 1 MES baseline costs before any
  subscription. There is no funded-account income to project from a negative
  edge — scaling, profit splits (80–90%), and payout rules multiply a
  negative number.
- **Eval economics at the measured edge:** $100–300/mo subscription burn ×
  (median ~1–2 months to bust at tradeable size, ~80% bust rate for F1/F2,
  reset fees on each bust; F3 burns subscription indefinitely without
  resolving). Probability-weighted cost of attempting evals with these
  strategies: roughly the full subscription + reset spend with ≈0 expected
  payout. **Do not buy an eval for this.**
- What WOULD make the math work (for a future iteration): the only verified
  raw material here is F3's ~0.9 gross pts/trade trend-day edge. It needs
  either (a) cost leverage — the identical signal on ES instead of MES cuts
  cost-per-$-of-edge ~4× ($2.48 + ~3 ticks on a $50/pt contract ≈ 1.5% of a
  45-pt gross year vs ~12% on MES), or (b) a structurally larger per-trade
  move (longer holds, wider trails, overnight component), or (c) a different
  entry concept entirely. Any of those is a NEW validation, subject to the
  same gates, on longer data.

## Validation debt / what would be needed before any retry

1. **More history**: 12 months (231 clean sessions) spans only one crash and
   one grind. The cache schema supports extending ES.c.0 1-min cheaply if
   the user ever re-authorizes a small Databento spend (the legacy pull cost
   pennies per day); 2–3 more years would cover 2023–24 regimes. (Not done —
   zero-spend rule.)
2. Fix the prefetch chunk-boundary truncation bug before any re-pull
   (12 sessions lost to `end=T16:05` UTC parsing).
3. If F3 is rebuilt: validate on ES economics, add the 4 missing expiry
   Fridays, and pre-register trail/hold parameters — n=94 is underpowered
   for anything subtler than the pass/fail answer it gave here.

## Files

- `futures_validation/extract_bars.py`, `es_1m.parquet` — extraction + QC
- `futures_validation/build_regime_futures.py`, `regime_daily.csv` — regime (yfinance, free)
- `futures_validation/sim_futures.py` — harness + F1/F2/F3 (declared rules in docstring)
- `futures_validation/diagnostics.py` — spot-check + cost decomposition
- `futures_validation/run_eval_overlay.py`, `eval_overlay_results.json` — eval sim
- `futures_validation/summary_futures.json`, `trades_*.csv`, `equity_paths.npy` — full results
- Droplet: read-only inspection only; no state touched. Total new spend: $0.
