# Advisor Memory — recommendation ledger

Every recommendation Codex ever makes is recorded here and **never deleted** — only updated. This is
how the system avoids forgetting, and how a recommendation's real-world outcome feeds back into
confidence. Maintained by Codex per `reports/CODEX_REVIEW.md`; reviewed and acted on by a human.

**Status lifecycle:** `Pending` → (human approves & deploys) `Implemented <date>` → (results in)
`Outcome: <measured effect>` with an updated confidence.

Newest first. One row per recommendation.

---

| Date | Recommendation | Reason (evidence) | Confidence | Status | Implemented | Outcome |
|---|---|---|---|---|---|---|
| 2026-07-21 | Export the active quantity-cap fields in standardized decision reports and add an end-to-end nonblank telemetry regression test. | Code defect; 743/743 decision rows have blank legacy proposed/permitted/headroom columns while the orchestrator emits requested qty, final qty, binding exposure, limit, remaining capacity, and incremental risk. Known limitation: dry run did not inspect production logs or patch code. Status: Observed. | Very High | Pending | — | — |
| 2026-07-21 | Keep non-Monday entries experimental/paper-only; retain Monday-only as the validated core until a pre-registered non-Monday variant passes OOS and 2x-cost gates. | Strategy flaw; prior 246-trade replay shows Monday PF 1.86 and 1.39 at 2x costs versus all-days PF 1.03 and 0.76. Tuesday's $293.60 realized gain came entirely from Monday-origin positions; Tuesday entries ended only +$20 unrealized. Known limitations: one-year prior sample lacks a sustained bear regime and today's fills are sandbox-unaudited. Status: Inferred from replay plus observed attribution. | High | Pending | — | — |
| _example_ | Increase ATR cushion to 1.25 | Similar afternoon reversals on 07-02 and 07-20 | Moderate | Pending | — | — |
| _example_ | (after implementation) same | — | High (raised) | Implemented | 2026-08-04 | Reduced avg loss 17% |

<!-- Codex: append real rows above this line. Keep both _example_ rows as the format reference, or
     delete them once real entries exist. Never delete a real recommendation — update its Status,
     Implemented, Outcome, and Confidence in place. A recommendation that was tried and failed stays,
     marked with its outcome, so it is not re-proposed blindly. -->
