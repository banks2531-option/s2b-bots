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
| _example_ | Increase ATR cushion to 1.25 | Similar afternoon reversals on 07-02 and 07-20 | Moderate | Pending | — | — |
| _example_ | (after implementation) same | — | High (raised) | Implemented | 2026-08-04 | Reduced avg loss 17% |

<!-- Codex: append real rows above this line. Keep both _example_ rows as the format reference, or
     delete them once real entries exist. Never delete a real recommendation — update its Status,
     Implemented, Outcome, and Confidence in place. A recommendation that was tried and failed stays,
     marked with its outcome, so it is not re-proposed blindly. -->
