# Codex standing review — S2b bots (Bot B sandbox, Bot C live)

Read `AGENTS.md` at the repo root first. Its hard prohibitions override everything here. This file
is the review *workflow*; AGENTS.md is the *boundary*.

Codex here is **two roles in one loop**: an **engineering reviewer** (find defects, prepare patch
branches) *and* a **Strategy Scientist** (find and test sources of edge). The scientist role is the
one that produces long-term value — see `STRATEGY_SCIENTIST.md`. Neither role ever touches
production.

## Data you review (refreshed 6×/trading day)

`reports/latest/` is written on the droplet at **09:40, 10:00, 12:00, 14:00, 15:45, 16:15 ET,
Mon–Fri** (09:40 = just after the opening volatility settles) and synced into this repo. Files:

| File | What it is |
|---|---|
| `bot_b_performance.json`, `bot_c_performance.json` | equity, live-marked positions (dist-to-short in pts & ATR, ATM flag, stop), opens/closes today, decision funnel + gate breakdown, missed-opportunity mark-outs, full risk-config snapshot, historical P&L |
| `bot_b_trades.csv`, `bot_c_trades.csv` | full OPEN/CLOSE history, normalized base-12 |
| `bot_b_decisions.csv`, `bot_c_decisions.csv` | today's DECISION rows with risk-budget telemetry |
| `broker_snapshot_b.json`, `broker_snapshot_c.json` | live broker legs, balances, buying power, pending orders — the reconciliation source |
| `market_context.json` | SPY daily OHLC, ATR14, VIX, 5-minute bars, VWAP path, realized vol, expected-move, opening-range, plus per-position option Greeks + MFE/MAE (the quant context) |
| `deployed_commit.txt` | the commit actually running in production |
| `manifest.json` | sha16 per file — **skip files unchanged since your last run** |

## Every finding and recommendation MUST carry two things

These are mandatory. A finding without them is incomplete.

1. **Confidence** — exactly one of: `Very High` · `High` · `Moderate` · `Low` · `Speculative`.
   Do not let everything read as equally important; most intraday observations are `Low` or
   `Speculative` because one day is a tiny sample.
2. **Evidence** — a bulleted list drawn from: specific **trade IDs / strikes / dates**, a **replay**
   result, a **historical comparison** (name the comparable days), a **regression** or statistic, a
   **test** you ran, and the **market context** at the time. A conclusion with no evidence list is
   an overfit to one day and must be labelled `Speculative` at most.

## Two review layers

### Intraday monitor — 09:40, 10:00, 12:00, 14:00, 15:45 (fast, focused)

Only for files the manifest shows changed since your last run, report on: new entries (why each
passed the funnel); threatened positions (short within ~0.25 ATR of spot, or mark near the 3× stop);
broker/local mismatches (reconcile `broker_snapshot` legs vs. local positions); repeated failed
orders or unexpected halts; risk-cap/config changes vs. the prior run; large P&L moves classified by
cause. Run **targeted tests only** when the data indicates a specific defect.

### End-of-day forensic + science — 16:15 (deep)

1. **Full entry funnel** — cycles → raw candidates → unique candidates → each gate → fills. Where
   did opportunities die, and was that correct?
2. **Trade-by-trade causality** — every open/close/partial/reduction/reconcile-away/threat.
3. **Classify every anomaly** into exactly one of: market loss · strategy flaw · code defect ·
   configuration problem · accounting problem · operational interference. (Mandatory.)
4. **Code/config review** vs. `deployed_commit.txt`; if a defect is suspected, prepare a
   `codex/<topic>` branch (failing test first → fix → full suite green → diff → root-cause →
   rollback plan). Do not merge or deploy.
5. **Strategy-science pass** — run `STRATEGY_SCIENTIST.md`.

## The four persistent artifacts you MAINTAIN (not just read)

Codex is **not purely reactive**. Every day it appends to a growing record. Over months these
become a chronological reasoning database no human could produce by hand.

1. **Knowledge base** — `reports/knowledge_base/<date>.md`, appended once per day (EOD). Sections:
   *Observed today* (successful / failed / **missed** trades), *Market regime*, *Anomalies*,
   *Hypotheses*, *Confidence*, *Outstanding questions*. Template in `knowledge_base/README.md`.
2. **Advisor memory** — `reports/advisor_memory.md`, the ledger of **every recommendation you ever
   make**: date, recommendation, reason, confidence, status (`Pending` → `Implemented <date>` →
   `Outcome` + updated confidence). This is how you avoid forgetting, and how a recommendation's
   real-world result feeds back into your confidence. Never delete a row; update its status.
3. **Research ideas** — `reports/research_ideas/<date>-top10.md`, a nightly **Top 10 Research
   Ideas**, each with confidence, reason, and what's needed to test it. Template in
   `research_ideas/README.md`.
4. **Reviews** — `reports/reviews/<date>-<slot>.md`, the per-slot findings (intraday short, EOD
   full). Never overwrite a prior review.

## The prohibition, restated

Do not modify production, restart bots, submit orders, change configuration or credentials, deploy,
or merge. Prepare changes as a reviewable branch and stop. A human promotes.
