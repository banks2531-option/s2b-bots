# S2b A/B Dashboard

Self-contained dashboard for the two S2b bots — **Bot A (Monday-only)** vs **Bot B (all-days)** —
modeled on the legacy `trading-bot` dashboard. A Python generator SSHes the droplet, pulls live
state + trade logs + Tradier sandbox marks for both bots, and writes a single auto-refreshing
`dashboard.html`.

## Run

```bash
cd dashboard
python build_dashboard.py --once                 # generate once, then open dashboard.html
python build_dashboard.py --watch                # regenerate every 60s
python build_dashboard.py --watch --interval 30  # custom interval
```

Then open `dashboard/dashboard.html` in a browser. With `--watch` running, the page self-refreshes
(`<meta refresh>`) every `interval` seconds.

## What it shows

- **Header:** SPY / VIX market context, generation time.
- **Overview tab** — per bot: status pill (RUNNING / HALTED / STOPPED), equity, realized today,
  unrealized, net day, all-time realized, entries-today/open; open-positions table (spread, expiry,
  credit, live mid, unrealized, cushion-to-short in points); today's filled activity; a count of any
  historical rejected/errored close attempts in the log.
- **Trade History tab** — per bot: closed-trade count, win rate, net realized, avg/trade, and a
  sortable table of all *filled* closes (click any header to sort).

Only `status == filled` closes count toward P&L/win-rate stats, so the storm's rejected rows in
`trades_alldays.csv` don't pollute the numbers (they're surfaced as a separate "rejected attempts"
note).

## Config (top of `build_dashboard.py`)

```python
SSH_KEY = "~/.ssh/id_ed25519_do"   # key that authenticates to the droplet
DROPLET = "root@159.89.45.162"
```

Tradier keys are read from `s2b.env` **on the droplet** and never leave it — the dashboard only
receives computed marks/equity over SSH.

## Data contract (droplet `/root/s2b-bot`)

| File | Used for |
|------|----------|
| `state_monday.json` / `state_alldays.json` | open positions, halt state, entries-today |
| `trades_monday.csv` / `trades_alldays.csv` | realized closes + today's activity (cols: event,date,ticker,short,long,expiry,qty,credit,action,exit_value,pnl,status) |
| `s2b.env` | Tradier token + sandbox base url (droplet-only) |
| `systemctl is-active s2b-{monday,alldays}` | process health |
