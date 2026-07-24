# S2B Deterministic Replay Harness — Design (2026-07-23)

**Status:** awaiting approval (user + advisor). No production/bot code is written until this is approved.
**Branch:** `codex/2026-07-23-replay-harness`
**Author:** Claude (Opus 4.8), at user direction, following the advisor's replay-process spec
(`S2B_Replay_Process_Comprehensive_Instructions.txt`).

**Chosen path:** *"Archive now, replay forward."* Ship decision-input archiving to the live bots first
so future sessions are fully replayable; build the harness on the bots' real functions; run the
advisor's full staged process from cleanly-captured sessions forward. July 23 is treated as a
**best-effort reconstruction for hypothesis generation only** — it cannot meet the deployment-grade
baseline gate (see §"Missing data").

---

## 1. Goal & non-goals

**Goal.** Answer, offline and reproducibly: *"Given the exact market data and bot state at each
decision point, what would the bots have done under alternative rules?"* — with a baseline that
faithfully reproduces real decisions before any counterfactual is trusted.

**Non-goals (hard).** The harness NEVER: contacts Tradier or any external service, places/modifies/
cancels an order, reads/writes production, uses live credentials, restarts a bot, or changes live
config. It runs entirely from archived files. It does not deploy anything (that is a separate,
human-gated step). This mirrors `AGENTS.md` Levels 1–2.

---

## 2. Architecture — two phases

### Phase A — Decision-input archiving (small patch to the bot, OFF by default)

The bot already logs decisions + risk telemetry, but **not** the option chain it saw, the SPY spot,
or (for rejected candidates) the strikes. Without those, no faithful baseline is possible. Phase A
closes that gap for **future** sessions.

- **Where.** `bot/app/orchestrator.py :: run_entry_cycle`. The chain is fetched at
  `orchestrator.py:1019` (`chain = deps.get_chain("SPY", expiry)`) and the candidate at `:1020`
  (`order = build_spread_order(spot, atr, chain, deps.s2b_cfg)`). Every decision outcome already
  funnels through the nested `_log_decision(reason, spot, atr, expiry, order)` (`:843`).
- **What we capture, per decision record (JSONL):**
  `ts_et`, `bot`, `deployed_commit`, `config_hash`, `spot`, `atr`, `expiry`, `regime`,
  the **full chain snapshot** actually used (`strike, bid, ask, mid, delta, iv` per leg, from the
  `OptionQuote` objects in `chain`), the built candidate (`short/long/qty/credit`), the
  `decision reason`, `limiting_gate`, `final_qty`, and — for Bot C — both `recorded_credit` and (when
  a fill exists) the broker `actual_fill_credit`. Account equity/BP and open positions carried in
  are already in `state`; we snapshot the relevant fields.
- **Safety (copied verbatim from the `_record_markout` pattern, `orchestrator.py:798-841`):**
  - New feature flag `Features.replay_capture: bool = False` in `bot/features.py` — **off by
    default**; enabled per-bot only after review. Orthogonal to `decision_logging`/`markout_tracking`.
  - Entire capture wrapped in `try/except: pass` — it can **never raise into the trading loop**.
  - Written through a new sink `deps.replay_log` built by `wiring.make_replay_capture_logger(path)`,
    mirroring `make_markout_logger` (`wiring.py:91`): append-only, rotate-on-schema-change.
  - **Throttling.** The lightweight decision fields are captured every cycle; the **full chain
    snapshot** is captured at most once per `(expiry, 15-min bucket)` using the existing
    `bucket15_of(now)` dedup — bounding log growth while preserving replay fidelity (a chain moves
    little within 15 min; decisions in that window replay against the bucket's snapshot).
- **Storage.** `reports/replay_capture/<bot>/<date>.jsonl` on the droplet, pulled by the existing
  `sync_reports.ps1` path. Zero behavior change to trading; pure observation.

### Phase B — Offline replay runner (new, isolated; calls the bot's REAL functions)

- **Location.** `replays/` (new top-level dir). Runner `replays/replay_runner.py`.
- **How it achieves fidelity.** It constructs a `Deps` (`orchestrator.py:114`) whose data callables
  are backed by the **archived** capture instead of Tradier:
  - `get_chain(symbol, expiry)` → returns the archived `OptionQuote` list for that `(expiry, bucket)`.
  - `option_quotes` / `option_greeks_iv` → served from the same snapshot.
  - `get_expirations(today)` → archived expiry set.
  - Order/trade sinks (`trade_log`, `markout_log`, `replay_log`) → in-memory collectors.
  - **No broker callable is wired** — any order-placement path is a hard `raise` in the fake deps,
    so a coding mistake can never reach a broker.
  Then it calls the bot's **actual** `tick(state, deps, now)` / `run_entry_cycle(...)` over the day's
  timestamps. This is what satisfies the advisor's "same strategy and risk functions" requirement.
- **Why not `simulations/engine_v345.py`.** That engine is a **standalone reimplementation** (stdlib
  only; does not import `bot/app`). It cannot be the fidelity baseline because it can silently
  diverge from the live logic. It remains useful for broad historical scanning, not for baseline
  reproduction.
- **Determinism.** Given identical archived inputs + config, the runner must produce identical
  decisions every run. Enforced by regression tests (§7). No wall-clock, no network, no RNG.

---

## 3. Required input files

Per replay date, an **immutable, hashed** package under `replays/<date>/source/`:

- `chain_capture.jsonl` — the Phase-A capture (chain snapshots + decisions).
- `trades.csv`, `markouts.csv` — the bot's trade + markout logs for the day.
- `state_open.json` — positions/equity/BP carried into the day (from `state_*.json`).
- `config.json` + `config_hash` — the exact `LIVE_FEATURES` / risk config in force.
- `deployed_commit.txt` — the running commit.
- For Bot C: `bot_c_credits.json` = `{recorded: 0.74, actual_fill: 1.21, stop: 2.22}`.
- `manifest.json` — sha16 per file (reusing `gen_report.sha16`), so inputs cannot silently change.

## 4. Missing data (honest gaps)

- **July 23 has no Phase-A capture** (it didn't exist yet). For 7/23 we can reconstruct: accepted
  trades + risk-gate math (fully logged), and an **approximate** chain by re-pulling historical OPRA
  quotes (Databento) or the `simulations/api_cache_overlay.db`. This is *reconstructed*, not the
  exact quotes the bot acted on — so the 7/23 baseline will carry documented mismatches and is
  **hypothesis-only**, never a deployment input.
- **Rejected-candidate strikes/timestamps are not in today's logs** (`gen_report.py:35` documents
  this). Phase A fixes it going forward; for 7/23 rejected paths are only partially reconstructable.
- **Quote timestamps** aren't carried by `parse_chain`/`OptionQuote` yet (`orchestrator.py:1053`).
  Phase A captures the fetch time; exact per-quote staleness remains unavailable.

## 5. Baseline reproduction criteria (acceptance gate)

A replay date is "baseline-valid" only if, for that date, the replay matches the real log on:
selected strikes, entry timestamps (±1 bucket), quantities, accept/reject decision, binding gate,
stop values, and exits. Small quote deltas are acceptable **only** on reconstructed (pre-Phase-A)
dates and must be explained. **Counterfactuals from a non-valid baseline are not used for any
deployment decision** (advisor's Final Priority).

## 6. Experiment matrix (one variable at a time)

Each writes `replays/<date>/results/<name>.md` with: rule changed, baseline vs counterfactual
behavior, trades prevented/added, strikes/times, simulated fills, realized+unrealized P&L, MAE, MFE,
peak portfolio risk, stops triggered, missed favorable ops, limitations, conclusion, confidence.

| ID | Experiment | Key measure |
|----|-----------|-------------|
| A  | Short-strike distance +0.25 / +0.50 ATR | would 740/741/742 be rejected/deepened? |
| B  | Adjacent-strike concentration limit (min gap vs open shorts) | does Bot B still stack 740/741/742? |
| C  | Portfolio downside-stress limit (reject if combined stressed loss > X% equity) | tail exposure |
| D  | Entry timing (+15m, +30m, post-VWAP-stabilize, no-entry-after-sharp-drop-from-high) | ITM-at-close rate |
| E  | $5 vs $10 wing on identical signals | credit / capital / drawdown |
| F  | Bot C actual-fill accounting + stop variants: `3×recorded(2.22)` vs `3×actual(3.63)` vs `min(credit_mult, max_$loss_cap, portfolio_limit)` | $ at risk, would-stop, MAE |

Experiment F is treated carefully: a wider stop is **not** automatically safer (it raises dollars-at-
risk-before-exit). The likely recommendation is a dollar-loss cap layered on the credit multiple.

## 7. Testing

Deterministic regression tests in `replays/tests/`: (1) identical inputs+config → identical
decisions (byte-equal decision stream); (2) the fake `Deps` raises on any order/broker call; (3) a
tiny fixture day reproduces its known outcome; (4) `spread_broker_credit`/stop math parity with the
live path. Full `bot/tests` suite must stay green.

## 8. Staged deployment (maps to advisor Stages 1–5) — all human-gated

1. **Workstation only** — harness runs from archived files on this branch. No prod/broker.
2. **Historical batch** — after a baseline validates, run experiments across ≥20–30 clean captured
   sessions (target 50–100+), multiple regimes, win/loss + hi/lo-vol days. One day ≠ justification.
3. **Sandbox shadow** — a validated rule computes what it *would* do alongside the live rule (no
   order); log production vs shadow decision + outcome for several sessions.
4. **Bot B sandbox pilot** — 1 contract, explicit rollback, no simultaneous unrelated changes,
   fixed review window, before/after metrics.
5. **Bot C live** — only after sandbox+shadow success, 5-execution actual-fill reconciliation, a
   dollar-loss cap, no broker/local mismatch, explicit approval. **The accounting fix ships
   separately from any strategy/stop change** — never bundled.

## 9. Order of execution

1. Phase A capture patch + tests (OFF by default) → review → user enables on Bot B first, then C.
2. Phase B runner skeleton + fake `Deps` + determinism tests.
3. Best-effort 7/23 reconstruction (reconstructed chain, documented caveats) → hypotheses only.
4. Once real captures accumulate: baseline-validate, then run experiments A–F.
5. Historical batch → shadow → pilots, per §8, each with its own approval.

## 10. Safety controls (summary)

Off-by-default flag · never-raise capture · append-only sinks · no broker callable in replay deps
(hard raise) · no network/creds/prod in `replays/` · isolated branch · hashed immutable inputs ·
accounting fix decoupled from strategy changes · every deployment stage human-approved.
