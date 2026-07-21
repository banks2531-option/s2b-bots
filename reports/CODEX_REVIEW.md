# Codex standing review — S2b bots (Bot B sandbox, Bot C live)

Read `AGENTS.md` at the repo root first. Its hard prohibitions override everything here. This file
is the review *workflow*; AGENTS.md is the *boundary*.

## Data you review (refreshed 5×/trading day)

`reports/latest/` is written on the droplet at **10:00, 12:00, 14:00, 15:45, and 16:15 ET,
Mon–Fri**, and synced into this repo. Files:

| File | What it is |
|---|---|
| `bot_b_performance.json`, `bot_c_performance.json` | equity, live-marked positions (distance-to-short in pts & ATR, ATM flag, stop), today's opens/closes, decision funnel + gate breakdown, missed-opportunity mark-outs, **full risk-config snapshot**, historical P&L summary |
| `bot_b_trades.csv`, `bot_c_trades.csv` | full OPEN/CLOSE history, normalized base-12 |
| `bot_b_decisions.csv`, `bot_c_decisions.csv` | today's DECISION rows with risk-budget telemetry |
| `broker_snapshot_b.json`, `broker_snapshot_c.json` | live broker legs, balances, buying power, pending orders — the reconciliation source |
| `market_context.json` | SPY daily OHLC, ATR14, VIX, and 5-minute bars |
| `deployed_commit.txt` | the commit actually running in production |
| `manifest.json` | sha16 per file — **skip files unchanged since your last run** |

## Two layers

### Intraday monitor — at the 10:00, 12:00, 14:00, 15:45 slots (fast, focused)

Only when the manifest shows a file changed since your last run, report on:

- **New entries** — for each, explain why it passed the funnel (credit tier, budget headroom, gap
  stress) using the decision telemetry.
- **Threatened positions** — any short strike within ~0.25 ATR of spot, or mark approaching the
  3× stop. Quote the numbers from `positions[]`.
- **Broker/local mismatches** — reconcile `broker_snapshot_*.legs` against the positions in
  `*_performance.json`. A leg count that isn't 2× the local spread count is a flag.
- **Repeated failed orders / unexpected halts** — `halted` true, or a halt_reason you can't explain.
- **Risk-cap or config changes** vs. the previous run's `config` block.
- **Large P&L moves** — realized or unrealized swings, classified by cause.

Run **targeted tests only** when the data suggests a specific defect (e.g. a reconcile mismatch →
run `bot/tests/test_reconciled_away_accounting.py` and the fill-accounting tests). Do not run the
full suite intraday unless a defect is indicated.

### End-of-day forensic — at the 16:15 EOD slot (deep)

Produce a complete daily report:

- **Full entry funnel** — cycles started → raw candidates → unique candidates → each gate → fills.
  Where did the day's opportunities die, and was that correct?
- **Trade-by-trade causality** — every open, close, partial, reduction, reconcile-away, and threat.
- **Classify every anomaly** into: market loss · strategy flaw · code defect · configuration
  problem · accounting problem · operational interference. (This classification is mandatory.)
- **Code/config review** against `deployed_commit.txt` — if a defect is suspected, trace it through
  the modules and, at Level 2, prepare a `codex/<topic>` patch branch with a failing test first,
  the fix, the full suite green, a diff, a root-cause note, and a rollback plan.
- **Historical comparison** — is today consistent with the trend in the per-day P&L, or an outlier?
- **Replays/tests** — run the relevant `simulations/` replay or `bot/tests` when a hypothesis needs
  evidence. State explicitly when a claim is unverified.

## Output

Write findings to `reports/reviews/<date>-<slot>.md` (create the dir). Intraday entries are short;
the EOD entry is the full forensic report. Never overwrite a prior review — append the day's set.

## The prohibition, restated

Do not modify production, restart bots, submit orders, change configuration or credentials, deploy,
or merge. Prepare changes as a reviewable branch and stop. A human promotes.
