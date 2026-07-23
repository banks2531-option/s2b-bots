# S2b Dashboard Redesign — Design

**Date:** 2026-07-22
**Status:** approved-pending-spec-review

## Goal

Replace the stale 3-bot dashboard with a detailed, intuitive, always-current dashboard for the
**two live systems only** — Bot B (sandbox) and Bot C (LIVE) — sourced from the Codex reporting
pipeline. Reviewable at a glance: a non-technical reader should understand *why* each bot is
performing the way it is, see a brief evolving health verdict, drill into technical detail, review
Codex's recommendations, and read a detailed historical daily P&L (including 2026-07-22).

## Non-goals

- Bot A (Monday-only) is removed entirely — no references anywhere.
- No live-auto-updating in-page data (not possible: the only Artifact capabilities are downloads/mcp,
  neither can read the bot data). Freshness comes from rebuilding on the droplet's data cron.
- No changes to bot trading behavior; the dashboard is READ-ONLY over existing data.

## Data sources (all already on the droplet)

- `reports/latest/bot_{b,c}_performance.json` — equity, realized fields, positions, funnel,
  gate_breakdown, history.by_day, config, pnl_source/validation, missed_opportunities.
- `reports/latest/broker_snapshot_{b,c}.json` — reconciliation (owned/foreign legs).
- `reports/latest/market_context.json` — SPY OHLC, VIX, session_vwap, regime inputs.
- `reports/latest/bot_{b,c}_trades.csv` — full OPEN/CLOSE history for trade-by-trade + daily P&L.
- `reports/advisor_memory.md` — Codex recommendation ledger (the Recommendations tab).
- `reports/reviews/<latest>.md` — Codex's executive conclusion (qualitative "why" enrichment).

## Delivery — separate site on the existing monarch VPS (NOT joined to the finance app)

The S2b dashboard is its OWN website on its OWN subdomain with its OWN password. It reuses the
monarch VPS + Caddy install but is otherwise fully independent of the finance app: no route added to
the FastAPI app, no code in the monarch repo, no nav links, no shared login/session.

- **Build (bot droplet):** new `dashboard/build_dashboard.py` (in the fable-options repo, full
  rewrite) reads the sources above on the bot droplet and writes a single self-contained
  `dashboard.html` (inlined CSS/JS, no external requests) to
  `/root/s2b-bot/reports/latest/dashboard.html`. Hooked into the existing `gen_report` cron so it
  rebuilds on every 6×/day data pass.
- **Transport (pull):** a systemd timer on the monarch VPS `scp`s `dashboard.html` from the bot
  droplet into a served dir (e.g. `/var/www/s2b/dashboard.html`), mirroring the `monarch-refresh-*`
  timers. Uses a read-only SSH key from the monarch VPS → bot droplet (restricted to the reports
  dir). VPS-side; user-run.
- **Serve (Caddy on the monarch VPS):** a NEW, separate Caddy site block for a distinct obscure
  subdomain (e.g. `s2b-<rand>.<domain>`), `basic_auth` with its OWN password (`caddy hash-password`),
  `file_server` on `/var/www/s2b/`, auto-HTTPS via the existing Let's Encrypt setup. The finance
  subdomain/site block is untouched.
- **Result:** a second bookmarked URL + second password, always current on the cron+pull cadence,
  any device; entirely separate from the finance site.
- VPS-side steps (Caddy block, pull timer, SSH key) ship as a runbook + config files in the
  fable-options repo's `deploy/`; the user runs them on the VPS (like the monarch SETUP.md).

## Content — three tabs

### Tab 1 · Overview (at-a-glance, plain English)
- Market strip: SPY last/%, VIX, session VWAP, one-line regime ("quiet, closed below VWAP").
- Two bot cards (B, C). Each shows:
  - **Health verdict** (see below) — status word + 2-3 sentence evolving assessment.
  - Equity · Today's realized (honest `realized_report_date`; C flagged *synthetic*) · Lifetime ·
    Win% · Open/Opened/Closed counts · P&L-source badge (actual vs synthetic).
  - A generated **"why" line** from funnel + gate_breakdown + the latest Codex exec conclusion.
- Daily-P&L sparkline per bot (last ~10 clean trading days).

### Tab 2 · Performance (technical, per bot)
- **Historical daily P&L**: table (date, realized, cumulative) + cumulative line chart, incl. 07-22.
- Win/loss, win%, profit factor, best/worst, avg; recent trade-by-trade (from trades CSV).
- Entry funnel (cycles → raw → unique → decisions → fills) + gate_breakdown (why blocked/filled).
- Missed-opportunity mark-outs; live risk-config snapshot; broker reconciliation (owned/foreign).

### Tab 3 · Codex Recommendations
- `advisor_memory.md` ledger rendered as a clean table, grouped **Pending** vs **Implemented**:
  date, recommendation, reason, confidence, status, implemented-date, outcome.

## Health verdict (the "brief cumulative, evolving assessment")

Per bot, generated each build (transparent rules, not a black box), rendered as a **status word**
(`Improving` / `Steady` / `Under pressure` / `Stalled` / `Insufficient data`) plus a **2-3 sentence
narrative** synthesizing:
1. **P&L trajectory** — direction + magnitude of recent clean daily P&L (Bot B pre-2026-07-20 history
   excluded as corrupted).
2. **Win-rate / profit-factor** — current level and recent movement.
3. **Execution health** — is it entering trades, or stalled by risk caps (e.g., Bot C pre-07-22)?
4. **Edge context** — Monday-only is the validated edge (PF ~1.86); all-days is diluted (PF ~1.03);
   flag when recent entries are non-Monday.
- Always closes with an **honest sample caveat** (thin data; synthetic P&L for Bot C).
- Enriched with a trimmed echo of the latest Codex review's "Executive conclusion" so it reflects
  Codex's own evolving daily assessment.

## Testing

- `bot/tests/test_build_dashboard.py`: pure helpers unit-tested — `health_verdict()` (each status
  branch), daily-P&L series builder (excludes corrupted Bot B pre-07-20 rows, includes 07-22),
  recommendation-ledger parser (Pending vs Implemented split), "why line" generator. Rendering is
  smoke-tested (builds valid HTML from fixture data, contains both bots, no "Bot A").

## Security

- Read-only static page; no secrets in the HTML. Its own obscure subdomain behind HTTPS +
  Caddy `basic_auth` (its OWN password), fully separate from the finance site — no shared login,
  session, subdomain, or nav. Account numbers stay masked as in the source JSON.
- The monarch VPS pulls over SSH with a read-only key restricted to the bot droplet's reports dir.
  The bot services/config are untouched. The monarch finance app, repo, and secrets are neither
  modified nor read by anything S2b.
