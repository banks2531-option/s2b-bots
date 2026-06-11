# Trading-Bot Fleet Diagnosis — 2026-06-10

Sources: droplet 159.89.45.162 (read-only SSH), nightly Tradier truth-reconcile data, bot logs/configs, local archive `E:\BanksBackup\trading-bot`, broker order export. All P&L figures below are **broker truth** (Tradier account history / IBKR paper fills), not bot-internal logs — the bot logs were proven unreliable (see §4.2).

---

## 1. Headline: where the fleet actually stands

| Bot | Mode | Broker-truth result | Current status |
|---|---|---|---|
| **livebot-b** (`/root/trading-bot`) | **LIVE** Tradier acct `6YB7****` | **−$1,405.28** since Feb 27 (81 spreads, 55.6% WR). Account: $2,000 → **$402.18** (−80%) | Running 24/7, **zero orders since May 7** — entry funnel mathematically sealed |
| **livebot-b-fork** | Sandbox (gate test) | n/a (paper) | Running, also zero new entries since May 28 |
| **daytrader-multi** (`/root/trading-bot-daytrader`) | Tradier sandbox $5k | **−$246 realized** (33 trades, 42% WR) **+ ~−$524** in two positions that expired worthless and got stuck | **Frozen since May 29**: both position slots occupied by phantom expired options; cannot enter trades. UW feed disconnected since May 27 |
| **ORB** (Docker, MES futures, IBKR paper) | Paper $9k | **−$493.75** (55 trades, 49.1% WR, Apr 23–Jun 10) | Healthy mechanically, negative expectancy strategically |

The often-cited "$25,000 account" in CLAUDE.md is aspirational. The real live account was funded at ~$1k, scaled to ~$2k, and now holds **$402**.

---

## 2. Per-bot root causes

### 2.1 livebot-b (live options credit spreads off Unusual Whales flow)

1. **Five weeks of silence: the Phase E calibrated-EV gate (added Apr 29) excludes the bot's own trade universe.** EV>0 requires raw probability ≥75–85% at the allowed credit ratios; the spread builder produces candidates at 60–72%. Result: 200–1,400 EV rejections per day, every day, since Apr 30. Last order: May 7 (timed out unfilled). The bot has burned droplet uptime producing nothing since.
2. **Negative expectancy even when it traded.** 55.6% win rate with average win $63 vs average loss $118 (payoff 0.54) = **−$17.35/trade**; breakeven needs 65.1% WR. Stops are disabled; the 50% take-profit caps winners while losers run to the 1-DTE forced close.
3. **Regime mismatch was the dominant dollar driver.** Identical strategy: **March +$607** (bearish tape) vs **April −$1,856** (bullish tape). The April post-mortem attributed ~94% of losses to REGIME_MISMATCH — $1M+ "bearish" UW flow during an uptrend is mostly institutional hedging, not directional conviction.
4. **Account too small for the structure.** 25% sizing on $402 ≈ $100 max risk vs $200–330 typical spread max-loss → "Position too large" rejections up to 400/day; fees (~$1.30/spread round trip) are a material % drag at 1-contract size.
5. **Bookkeeping drift corrupted every feedback loop.** May 28 drift report: **ALARM_HIGH, −$684 divergence over 30 days** between bot CSV and broker truth. Historic phantom-close loop (same XOM spread "closed" 29×) inflated the bot's logged balance to ~$72k while the broker said −$1.9k for April. The ML/adaptive-selectivity state trains on these wrong records. Since May 28 the nightly drift check reads "OK" only because *neither side has any trades* — it has masked a dead bot for 13 days.
6. **Security/config hazard:** the **live API key + account are hardcoded in `livebot_b.py:371-373` and override `.env`** (which misleadingly lists sandbox credentials).

### 2.2 daytrader-multi (Box/Dow intraday signals → 1-DTE long options, 21 underlyings)

1. **Stuck-state bug = dead bot.** Two options expired worthless in-state (GOOGL 5/20 C, SPY 5/29 C); the sandbox never filled 1,628 close attempts; there is no expired-contract cleanup; `max_open_positions: 2` means both slots are permanently occupied. Zero trades possible since May 29 — and the heartbeat reports "healthy" throughout.
2. **Inverted trade geometry.** Designed +100% TP / −25% SL, but in practice winners get clipped early by breakeven-move/trailing/time-stop (14 of 33 exits were time-stops; only 6 reached target) while stops take full size: avg win $22 vs avg loss $29 at 42% WR.
3. **Deployed against its own backtest verdict.** The Box/Dow minute backtest produced $29.63 total over 252 days and *failed the project's own go/no-go gate* ($0.93/day vs $30/day required); the filter blocked 4× more winners than losers. It was deployed anyway.
4. Config is a diary of 6+ gate-loosening rounds because the bot "wasn't trading enough" — tuning toward more trades from a negative-edge signal.
5. Only positive cohort: `box_breakout_v2` +$131 (n=7, PF 3.18) — small but consistent with trend-following beating counter-trend in this period.

### 2.3 ORB (15-min opening-range breakout, MES futures, IBKR paper)

1. **Expectancy geometry fails in execution.** Design: −$60 stop / +$80 target. Reality: average loss **−$78** (slippage/gap-through on 5-min bars), average win **+$63** (EOD hard-closes truncate winners). At 0.80 realized payoff it needs ~55% WR; it has 49.1%.
2. **The flagship setup is the entire deficit:** `orb_break` −$609 over 46 trades. `midpoint_reclaim` is +$171 on 3 trades (tiny n, but directionally consistent with regime findings). Choppy days produce stop-out strings.
3. Operational debris: 294 container restarts (nightly IB Gateway auto-restart at 23:45 → bot exits → docker revives), a −$312 naked-position incident from a bracket failure (Apr 30), early fill-attribution bugs.
4. Historical echo: Apr–May archive shows the same signature — ORB longs +$45 (PF 1.07) vs shorts −$431. Direction-insensitive breakouts in a trending bull tape lose on the short side.

---

## 3. Cross-cutting root causes (why the *project* underperforms, beyond any one bot)

1. **No regime awareness.** Every major loss cohort (April BCS, ORB shorts, daytrader zone_entry) is the same failure: direction-sensitive entries with no read on the prevailing trend. The archive's own findings — cyc-vs-defensive gate (BCS in cyclical-led tape: PF 0.05 vs PF 1.36 otherwise), IV≥35% BPS (79% WR vs 46% below) — were identified but only deployed as a dry-run experiment on May 28.
2. **Bot-internal records ≠ broker truth.** Phantom closes, 80% order-rejection storms going unnoticed (650 "Buy To Cover" rejects in the Feb–Mar export), settlement guessing, double log handlers. The truth-reconcile pipeline (the single best piece of engineering in the project) arrived after the damage.
3. **Systematic overfitting.** Every parameter sweep: in-sample PF 5–7 → out-of-sample PF 0.5–0.8, on 10–50-trade samples. The 0DTE "winners" sit next to losing siblings differing only in TP/SL.
4. **Go/no-go discipline collapses at deploy time.** Box/Dow deployed after failing its gates; PATH B v2 deployed despite PF 0.00 replays (then 20 days of zero trades); stacked changes instead of single-flag experiments. The May 27 dual-track gate protocol (dry-run + enforcing fork, predefined decision date of Jul 8) is the right template and arrived last.
5. **Monitoring measures process, not outcome.** Every bot reports "healthy" while doing nothing: livebot-b dead 5 weeks, daytrader frozen 9 sessions, drift check vacuously green. Nothing alerts on "zero trades for N days" or "position past expiry."
6. **Capital/structure mismatch.** Credit spreads at 1-contract scale on a sub-$1k account cannot overcome fees and lumpy max-loss sizing, regardless of signal quality.

### What actually worked (worth carrying forward)
- Feb 13–Mar 27 live era: **+$358–$379, 65–66% WR, PF ~1.34** (independently confirmed from the broker export) — the strategy in a favorable (bearish/volatile) regime.
- IV≥35% filter for bull put spreads (79% vs 46% WR).
- Cyclical-vs-defensive regime gate for bear call spreads.
- `midpoint_reclaim` and ORB-long cohorts; `box_breakout_v2`.
- The truth-reconcile pipeline and the May 27 dual-track/decision-date experiment protocol.
- Solid plumbing: Tradier/UW/IBKR integrations, bracket orders, prop-style risk manager, Docker/IBC gateway automation.

### External resource verdict
**cm-jones/thales: ignore.** Abandoned C++ scaffold that has never traded — the IB client is a stub (returns fake order IDs), the backtester's P&L is `initial_capital + 100 × trades`. Worth re-implementing fresh, in Python, are only two *patterns*: a mandatory pre-trade `is_order_allowed()` risk gate, and a clean strategy-registry/signal contract.

---

## 4. The honest math on $4–5k/month

$4–5k/month is a **capital question before it is a strategy question**:

| Capital base | Required monthly return for $4.5k | Realism |
|---|---|---|
| $402 (current) | ~1,100% | Impossible |
| $5,000 | 90% | Impossible sustainably |
| $25,000 | 18% | Top-decile *annual* returns, monthly; functionally gambling |
| $50,000 | 9% | Still far above what consistent algos deliver; high ruin risk |
| $100,000–$150,000 | 3–4.5% | Aggressive but within the realm of skilled systematic trading — and still not guaranteed |
| Prop-firm funded accounts (futures) | n/a — payout-based | The only structural path to $4–5k/mo without ~$100k personal capital: e.g. 2–3 funded $50–150k eval accounts trading MES/ES. Real but hard: most evals fail; payouts have rules; treat as a venture with ~$100–300/mo eval subscription burn |

No honest design gets $4–5k/month from the current $402. Anyone promising that rate of return on small capital is describing a lottery ticket. The realistic menu: (a) rebuild small and compound while proving edge, (b) pursue prop-firm funded futures accounts where the existing IBKR/MES infrastructure becomes the eval-passing engine, (c) fund a larger account only after a strategy has a ≥3-month verified live/paper record.

---

## 5. Recommended immediate triage (pending approval — these touch live systems)

1. **Stop the live livebot-b instance** (it has produced zero orders since May 7; it holds a hardcoded live API key; it provides no value while running). Keep the fork's dual-track gate experiment if desired — it's sandbox.
2. **Rotate the Tradier live API key** and remove hardcoded credentials from source.
3. **Purge the daytrader's two phantom expired positions** from `multi_positions.json` (or stop the bot) — it cannot trade until then, and add expiry cleanup if it keeps running.
4. **Leave ORB paper running** — it's generating clean experimental data at zero cost; just silence the after-hours log spam and fix the nightly reconnect crash loop later.
5. **Add a dead-man alert**: any "running" bot with zero trades for 3+ sessions, or any position past expiry, should page instead of heartbeating "healthy."

---

## 6. Replacement-bot directions (to be designed after capital/path decision)

**Option A — One clean options bot, rebuilt around the verified edges (Tradier).**
Single credit-spread bot: regime gate first-class (SPY trend + cyclical/defensive + IV≥35% for BPS), multi-signal corroboration (UW flow as *one* input, never sole trigger), proper stop geometry, broker-truth ledger from day 1, strategy-registry + pre-trade risk-gate architecture. Paper → live at small size. Realistic outcome: prove/disprove edge in 2–3 months, compound a small account; **cannot** reach $4–5k/mo on small capital by itself.

**Option B — Prop-firm futures track (funded-account leverage).**
Reuse the IBKR/MES plumbing (bracket orders, prop-style risk manager, Docker/IBC stack). Replace `orb_break` with the trend-aligned setups the data favors (longs-with-regime, midpoint_reclaim family), validate via the dual-track paper protocol with strict OOS go/no-go gates, then attempt evals (e.g. Topstep/Apex-class, $50k–150k). Structurally the only path to $4–5k/mo without ~$100k personal capital; comes with eval-failure attrition risk and payout rules.

**Option C (recommended) — Staged portfolio: A + B under shared discipline.**
Kill dead bots; build one shared core (broker-truth ledger, regime service, risk gate, dead-man monitoring); run Option A (options, paper-first) and Option B (futures, paper → eval) as two thin strategies on that core. Predefined PASS/ABANDON gates per stage (the May 27 protocol), scale only what passes. Matches the staged-commitment working style; produces an honest answer about $4–5k/mo feasibility within ~2–3 months instead of promising it upfront.
