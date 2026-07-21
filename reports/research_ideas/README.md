# Research ideas — nightly Top 10

One file per trading day: `reports/research_ideas/YYYY-MM-DD-top10.md`, written at the 16:15 EOD slot
by the Strategy-Scientist pass (`STRATEGY_SCIENTIST.md`). Ten ranked ideas. Over months this is where
the alpha comes from.

Format for each idea:

```markdown
### Idea N — <one-line title>
Confidence: <Very High | High | Moderate | Low | Speculative>
Reason: <what in today's / recent data motivates it — cite dates, trade IDs, numbers>
Test: <the specific experiment — which replay, which parameter sweep, which comparison>
Data needed: <what's missing, e.g. "≥500 candidate outcomes", "0.25-delta chain history">
Status: <New | Replay-queued | Tested → result | Promoted to advisor_memory>
```

Rules:
- An idea that survives a replay/test graduates to `reports/advisor_memory.md` as a `Pending`
  recommendation.
- An idea that fails is marked `Tested → rejected` and **kept** — a disproven idea is evidence and
  stops the same thing being re-proposed weekly.
- Confidence is capped at `Low` when the only support is a single day; it rises only with replay,
  regression, or multi-day/historical confirmation.
