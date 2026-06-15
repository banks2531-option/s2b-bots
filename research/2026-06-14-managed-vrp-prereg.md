# Pre-Registration — Managed-Risk Index-VRP Bot (vs validated S2b)

Date written: 2026-06-14 · Status: PRE-REGISTERED (no results). Decision date: **2026-06-21.**
Builds on: the 2026-06-14 bot-B dissection (payoff asymmetry, not entries, sank live trading) + the winning-strategies research (index VRP is the one capturable edge) + the validated **S2b** baseline (Monday SPY put spread, PF 1.86).

> Written before looking at any outcome. The H1-tune / freeze / H2-once discipline (C4/C5) is mandatory — parameter sweeping is the exact mechanism that produced the legacy bot's in-sample PF 5–7 → live 0.5–0.8 collapse.

---

## 0. What this tests — and explicitly what it does NOT

The bot-B dissection showed the loss came from **payoff asymmetry + wrong vehicle**, not weak entry signals — and that bot B's "intelligence" (ML, conviction, whale-premium, EV gate) was anti-predictive. So this experiment does **not** rebuild a multi-factor predictor. It tests the only two levers that are both (a) evidence-backed and (b) genuinely untested in this project, against the validated S2b baseline:

- **Lever 1 — Stop/TP payoff structure.** S2b currently runs TP 50% / stop 2.0× (you risk 2× credit to make 0.5× — asymmetric, carried by win rate). Does a different, less-asymmetric structure improve risk-adjusted return or cut the tail?
- **Lever 2 — VIX term-structure (contango) gate.** Enter only when front vol ≤ longer vol (VIX ≤ VIX3M = short-premium being paid). The single highest-evidence "free conditioner" from the strategy research, and **never tested here**. (Distinct from IV-rank, which failed 3× and is excluded.)

**Explicitly excluded (already disproven OOS — do not re-run):** options-flow direction, IV-rank conditioning, the ML multifactor stack, single-name vehicles, naked structures. **Not a research hypothesis but an implementation requirement:** the live bot's stops must actually *fire* (bot B's didn't — `scaled_stop_level` set on 0/114). The backtest already models firing stops; execution discipline is an ops gate, not tested here.

## 1. Hypotheses (pre-registered)

- **H1 (structure):** Some pre-declared (tp_pct, stop_mult) on the Monday SPY put spread beats S2b's 50%/2.0× on H2 (OOS) on the primary metric, robustly (a *plateau*, not a lone point optimum).
- **H2 (term gate):** Gating S2b entries to VIX-term-structure contango improves OOS risk-adjusted return and/or reduces the worst-trade tail vs ungated S2b.
- **Null (default belief):** neither lever beats the S2b baseline OOS once multiple comparisons are honestly penalized. Prior: S2b is already good and factor/parameter conditioning has repeatedly failed here.

## 2. Data & engine

- **Engine:** existing `simulations/sim_mech.py` — real Polygon 5-min **option** bars, empirical NBBO half-spread slippage, $0.65/leg commission, intrinsic settle. Reference strategy `spy_putwrite_weekly` = the baseline.
- **Window:** 2025-03-03 → 2026-02-27 (the harness's option-bar coverage). **Split:** H1 = first 50% of Mondays (in-sample, for tuning), H2 = second 50% (OOS, touched once). Same split convention as the validated runs.
- **Term-structure series:** VIX & VIX3M daily (yfinance `^VIX`, `^VIX3M`), point-in-time prior-session close; gate = `VIX_close ≤ VIX3M_close` (contango) evaluated the session before/at entry. **[feasibility risk: gating ~47 Monday trades will shrink n — power is the main threat; flagged in §6.]**
- **First-run cost:** ~430 paced Polygon calls/yr for the weekly spread (~4–5 h wall clock at the throttled ~5/min, then cached). VIX/VIX3M are free.

## 3. The sweep (small, pre-declared — anti-overfit)

To bound multiple comparisons, the parameter grid is fixed here, in advance:
- **tp_pct ∈ {0.50 (baseline), 0.35, 0.65}**
- **stop_mult ∈ {2.0 (baseline), 1.5, 1.0}**
- term gate ∈ {off (baseline), on}

= 3×3×2 = **18 configs**, one of which is the exact S2b baseline. **Tuned only on H1.** No other parameters move (entry time 10:00 Mon, 30Δ short, $5 width, weekly ≥4 DTE, time-exit 1 DTE all frozen at S2b values). No second grid, no re-sweep after seeing H2.

**Selection rule (on H1 only):** pick the single config maximizing the H1 primary metric **subject to a plateau check** — its neighbors in the grid must also rank in the top third on H1 (a lone spike with bad neighbors is rejected as overfit and we fall back to baseline). Freeze that one config. Evaluate it once on H2.

## 4. Primary metric & gate

**Primary metric = Profit Factor at 2× modeled costs** (the stress level the project uses), with **max single-trade loss** and **maxDD** as co-primary tail guards.

**PASS (the new config is worth a forward paper test) requires ALL, on H2 (OOS):**
1. OOS PF (2× costs) **> S2b baseline's OOS PF** on the same H2 Mondays, by a margin beyond bootstrap noise (block-bootstrap 95% CI of the PF *difference* excludes 0);
2. OOS worst-trade loss and maxDD **≤ S2b baseline** (no tail-for-return trade-off);
3. The winning config survived the §3 plateau check (not a point optimum);
4. n(H2 trades) ≥ 15 (power floor; if the term gate shrinks below this, that arm is **inconclusive**, not a pass).

**ABANDON / keep S2b** otherwise. A negative is a real result: "S2b's 50%/2.0× ungated structure is not beaten" is decision-useful and cheap.

## 5. Honesty instruments

- **H2 touched once**, after the single config is frozen on H1.
- **Baseline-relative:** every claim is vs S2b on the *same* H2 Mondays — not vs an absolute bar — so a generally good/bad market half can't masquerade as edge.
- **Block bootstrap by week** for the PF-difference CI (Monday trades are weekly, low overlap, but bootstrap anyway).
- **Multiple-comparison ledger:** 18 configs declared up front; selection on H1 only; the plateau check is the overfit guard; H2 is a single confirmatory test of one config.
- **Report the full H1 grid** (so the plateau is visible) and the one H2 number.

## 6. Known limitations (stated up front)

- **Small n.** ~47 Monday S2b trades total → H2 ≈ 23, and the contango gate cuts further. This is the dominant threat; a *positive* needs heavy discounting, a *clean negative* is robust. The n≥15 floor (§4.4) makes thin arms inconclusive rather than false-positive.
- **One year, one regime.** No cross-regime generality claim.
- **Modeled greeks for strike selection** (alert-IV-biased delta, ±0.04) — same proxy as the validated S2b runs, so baseline-relative comparison is fair.
- **0DTE/thin-wing optimism** does not apply (weekly ≥4 DTE, liquid SPY).

## 7. Decision rule, one line

Tune 18 configs on H1 → plateau-check → freeze one → test once on H2 vs S2b baseline. **Beat S2b on PF *and* tail, robustly, with n≥15 → forward-paper the managed config.** Else keep mechanical S2b as the deployment candidate and stop adding levers. Decision by **2026-06-21.**

### Artifacts (to be produced on approval)
- `simulations/managed_vrp_sweep.py` (drives `sim_mech.py` over the 18 configs + term gate; reuses the cached option bars)
- `simulations/term_structure.py` → `vix_term.csv` (VIX/VIX3M contango flag, PIT)
- `research/2026-06-14-managed-vrp-results.md` → verdict vs this prereg
