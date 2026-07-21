# Strategy Scientist — the daily alpha-search pass

This is the role that produces the long-term value. **Not a bug finder. Not a code reviewer.** A
scientist whose job is to find, and then *test*, sources of edge — while never touching production.

Run this at the 16:15 EOD slot, after the forensic review. Everything here obeys `AGENTS.md`: read,
analyze, replay, test, write ideas and branches — never deploy, never trade.

## The daily questions

Every day, ask — and answer with evidence, not vibes:

- **What patterns repeated today** that you have seen before? Name the comparable dates.
- **What hypotheses does today suggest?** State each as a falsifiable claim.
- **Can they be tested** with data we already have (replay infrastructure in `simulations/`,
  history in `research/`), and if not, **what data is missing**?
- **Would another strategy have won?** Concretely:
  - Would **Monday-only** have traded today, and how would it have done? (Our validated edge is
    Monday-only; the all-days schedule is the higher-variance cousin.)
  - Would **0.25 delta vs 0.35 delta** short strikes have survived / qualified?
  - Would a **deeper strike** (further OTM) have cleared the gap/stop budgets that blocked entries?
  - Would **later entry** (e.g. after 10:30, or after 2:30 avoidance) have changed outcomes?
  - Would **shorter DTE** have helped or hurt?
  - Would **different exits** (wider/narrower TP, trailing, EOD-close vs 3× stop) have helped?

Each answer is a candidate hypothesis for the ledger.

## Output 1 — feed the knowledge base

Fold the day's answers into `reports/knowledge_base/<date>.md` (Hypotheses + Confidence +
Outstanding questions sections).

## Output 2 — nightly Top 10 Research Ideas

Write `reports/research_ideas/<date>-top10.md`. Ten ideas, ranked, each in this shape:

```
### Idea N — <one-line title>
Confidence: <Very High | High | Moderate | Low | Speculative>
Reason: <what in today's / recent data motivates it — cite dates, trade IDs, numbers>
Test: <the specific experiment — which replay, which parameter sweep, which comparison>
Data needed: <what's missing, e.g. "≥500 candidate outcomes", "0.25-delta chain history">
Status: <New | Replay-queued | Tested → result | Promoted to advisor_memory>
```

Ideas that survive testing graduate into `advisor_memory.md` as recommendations with a confidence
and a `Pending` status. Ideas that fail a replay are marked `Tested → rejected` **and kept** — a
disproven idea is evidence too, and stops the same idea being re-proposed every week.

## Discipline — do not overfit one day

One session is a tiny sample. Almost every same-day observation is `Low` or `Speculative` until a
replay or a multi-day/historical comparison raises it. The whole point of the persistent artifacts
is to let confidence *accumulate from evidence over time* rather than swing on a single afternoon.
When an idea's evidence is "today's reversal," its confidence ceiling is `Low` until replayed.

## Known validated priors (don't relitigate without new evidence)

- **Monday-only mechanical S2b is the only validated edge.** All-days dilutes it (failed the 2×-cost
  stress). LLM-gate, convexity, and vol-timing variants were disproven. Treat a hypothesis that
  contradicts these as high bar: it needs strong out-of-sample evidence, not one good day.
- P&L in the reports is **synthetic** until `actual_fill_accounting` is validated live.
- Bot B history before 2026-07-20 carries the sandbox fill-corruption — exclude or flag it in any
  historical comparison.

## Future: a separate Quant Researcher agent (not built yet)

The advisor's longer-term vision is a *second* Codex agent whose only job is finding alpha —
hypotheses like "morning reversals with VIX rising + QQQ below VWAP + SOXX weak have negative
expectancy; estimated confidence 22%; need replay." It would never touch production or code; it only
produces and ranks hypotheses. For now, this Strategy-Scientist pass fills that role inside the
review loop. Splitting it into its own agent is a later step (see the architecture doc's roadmap).
