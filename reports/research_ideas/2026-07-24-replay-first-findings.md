# Replay harness — first real-data findings (2026-07-24)

**Source:** first Phase-A capture (`replay_capture_alldays.jsonl`, Bot B, 355 decision records) +
`markouts_alldays.csv` (906 usable rows). Analysis script: `research/replay_20260724_analysis.py`.

**Standing caveat (advisor's Final Priority):** this is ONE session. Every counterfactual/strategy
observation below is **hypothesis-generating only**, capped at **Low / Speculative** confidence. A
strategy change requires the historical batch (20–30+ clean captured sessions) the harness now makes
possible.

---

## Finding 0 — the replay harness reproduces real captured decisions (pipeline validated)

- **Classification:** n/a (tooling validation). **Confidence: High** (for what it covers).
- **Evidence:** replayed all 355 captured records through the bot's real `run_entry_cycle`.
  **Instant-determined decisions reproduce: 30/36** — `off_hours` 29/29, `filled` 1/1 (the 732/722
  entry rebuilt with the same strikes + sized qty). The one `filled` record carried the full 167-leg
  chain snapshot; the harness rebuilt the order exactly.
- **The 6 `quote_invalid` records: only 1 reproduced.** Root cause is a known fidelity limit: the
  chain is snapshotted once per 15-minute bucket, but `quote_invalid` fires on a specific instant's
  bad leg quote. The replay sees the bucket's (valid) snapshot, not the transient bad quote. **→
  Refinement (concrete):** also snapshot the chain on `quote_invalid`/`quote_wide` decisions, since
  those are exactly the instants where the raw quote is the whole point.
- **The 319 `risk_budget:gap_2atr` records did NOT reproduce under INDEPENDENT replay (0/319) — as
  expected, not a defect.** A gap-budget reject is a function of the accumulated open book; independent
  single-record replay starts from an empty book. This motivated the **stateful whole-day replay mode
  (now built — Finding 0b).**
- **Limitations:** quote timestamps aren't carried (v1); book-dependent gates need stateful replay +
  multi-expiry IV (Finding 0b).

## Finding 0b — stateful whole-day replay BUILT; gap_2atr baseline still gated on multi-expiry IV

- **Classification:** tooling. **Confidence: High** (mechanism proven by unit tests; the 7/24
  shortfall is precisely diagnosed).
- **What was built:** `replay_day()` threads ONE `BotState` chronologically through the day's records,
  seeded with the day-open book, so book-dependent gates respond to the accumulated positions.
  Unit-tested: the *identical* record that FILLS from a flat book is BLOCKED (`max concurrent
  positions reached`) when the book is seeded to the cap — proving the gate now reads the threaded
  state. A snapshot-backed IV resolver feeds captured per-leg IV into the BS gap-stress reprice.
- **7/24 result (seeded with the real day-open book 740/730, 741/731, 729/719):** gap_2atr STILL did
  not reproduce — the replay instead **admitted trades and hit `max concurrent positions reached`
  (299 records)**. Precisely diagnosed: the capture snapshots only the *candidate's* expiry chain
  (7/31), so the **held positions' expiries (7/29, 7/27) fall back to `gap_fallback_iv`=0.20**, which
  UNDER-estimates their 2-ATR gap stress → the aggregate gap budget looks looser than live → the
  candidate passes the gap cap and fills → the book grows to `max_open`. Live's real per-leg IV made
  gap_2atr bind; the replay's fallback IV did not.
- **→ Concrete next capture refinement:** Phase A must snapshot the chains for **every expiry present
  in the open book**, not just the candidate's expiry. That is the missing input for a deployment-grade
  gap_2atr baseline (advisor's "baseline must reproduce before counterfactuals are trusted").
- **Honest status:** the stateful ENGINE is validated; the 7/24 gap_2atr BASELINE is NOT yet met, so
  no gap_2atr counterfactual is drawn. We now know exactly what to capture to close it.

## Finding 1 — Q F: `gap_2atr` gates a narrow near-money band; the bot re-proposes it all day

- **Classification: configuration/strategy interaction. Confidence: Low** (one session, but the
  count is large and unambiguous).
- **Evidence:** 319 of 355 decisions were blocked by `gap_2atr`. Every blocked candidate's short sat
  in a **narrow band: strikes 730–736**, at a **median 1.07 ATR below spot** (min 1.00, max 1.30 ATR).
  Spot ranged 737.4–743.7 while blocking. The most-blocked shorts: 734 (67×), 731 (64×), 735 (57×),
  732 (40×), 730 (36×), 733 (35×).
- **Interpretation:** `gap_2atr` is doing its job — near-money shorts (~1 ATR OTM) carry high 2-ATR
  gap-stress, so the budget rejects them. But it reveals the strategy **repeatedly targets the same
  near-money, gap-fragile band** (~1.0–1.1 ATR OTM) and gets wall-rejected 319×, rather than seeking
  deeper strikes the budget would admit. This is the *mechanism* behind the advisor's Q A (more
  distance) and Q F (gap_2atr concentration): the two questions are the same coin — the bot fishes
  ~1 ATR out, which is exactly where gap_2atr bites.
- **Limitations:** one session; `gap_2atr` counterfactual (what deeper strikes would have done) needs
  the stateful replay above; the underlying signal that picks ~1-ATR strikes wasn't isolated.

## Finding 2 — Q A: favorable-to-seller rises with short-strike distance (weak, one session)

- **Classification: strategy signal. Confidence: Speculative** (60-minute markout, one down day).
- **Evidence (906 markout rows, 60m spread move; "favorable" = spread value fell):**
  - **6–9 pt OTM: 38% favorable (259/684)**
  - **9–12 pt OTM: 43% favorable (95/222)**
  - (No candidates <6 pt or ≥12 pt this session.)
- **Interpretation:** favorable-to-seller **increases with distance** (38% → 43%), directionally
  supporting the advisor's Q A (require +0.25–0.50 ATR more distance). The absolute rates are low
  because 2026-07-24 was a down day (SPY 743→737 intraday) that pressured put spreads, and this is a
  60-minute markout, **not** a full-hold outcome.
- **Limitations:** 60m ≠ hold-to-exit; one down session; no vol/regime control; distance buckets are
  coarse. Do NOT size a rule from this — it's a hypothesis to test across the batch.

## Finding 3 — Q B: the flagged adjacent-strike concentration is real and observed

- **Classification: market/strategy exposure. Confidence: Low** (direct observation, one session).
- **Evidence:** Bot B simultaneously held **740/730 and 741/731** — shorts **1 point apart**, same
  2026-07-29 expiry — plus 732/722. When SPY fell, both adjacent shorts went ITM together
  (−$165 / −$202 at the 15:45 snapshot). This is exactly the concentration the advisor flagged on 7/23.
- **Interpretation:** supports testing a "min gap between concurrent shorts" rule (advisor Q B). The
  bot has no rule preventing near-adjacent shorts stacking in one expiry.
- **Limitations:** one session; paper account; correlation of the two shorts is obvious but its P&L
  impact vs. a spacing rule needs the counterfactual replay.

---

## Prioritized next steps (what the data says to build/run next)

1. **DONE: stateful whole-day replay mode** (`replay_day()`). Engine validated; see Finding 0b.
2. **Multi-expiry chain capture** — Phase A must snapshot chains for every expiry in the open book,
   not just the candidate's. This is the ONE input still blocking a deployment-grade gap_2atr baseline
   (Finding 0b). Highest value next.
3. **Capture the chain on `quote_invalid`/`quote_wide`** too (not just once per bucket) — closes the
   quote-fidelity gap (Finding 0).
4. **Accumulate the historical batch** — let Phase-A capture run across 20–30+ sessions and multiple
   regimes before any A–F conclusion is trusted.
5. Then run experiments A (distance), B (adjacency spacing), F (gap_2atr) on the batch with the
   stateful replay.

No production strategy or risk change is recommended from one session. All findings above are inputs
to the batch study, not decisions.
