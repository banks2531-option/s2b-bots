# Knowledge base — daily chronological reasoning

One file per trading day: `reports/knowledge_base/YYYY-MM-DD.md`, appended once at the 16:15 EOD
slot. Over months this becomes a chronological reasoning database. Codex maintains it per
`reports/CODEX_REVIEW.md` and `STRATEGY_SCIENTIST.md`.

Copy this template for each day:

```markdown
# Knowledge base — <YYYY-MM-DD>

## Observed today
### Successful trades
- <bot> <strikes> x<qty> — entry <credit>, exit <value>, P&L <$> — why it worked (evidence)
### Failed trades
- <bot> <strikes> — what went wrong, classified (market loss / strategy flaw / code defect / config / accounting / operational interference)
### Missed trades (blocked candidates worth noting)
- <bot> <strikes> blocked by <gate> — 60-min mark-out moved <±> (would it have won?)

## Market regime
- SPY <chg%>, ATR14 <>, VIX <>, VWAP path <above/below>, realized vol <>, expected move <>, breadth <>
- Regime label: <trend / chop / reversal / risk-off …>

## Anomalies
- <anything that didn't fit — a gate firing unexpectedly, a reconcile drift, a data gap>

## Hypotheses (each falsifiable, each with a confidence)
- H: <claim> — Confidence: <Very High|High|Moderate|Low|Speculative> — Evidence: <dates/IDs/stats>

## Confidence in today's read
- <overall: how much of this is signal vs. one-day noise>

## Outstanding questions
- <what you could not resolve with the data on hand, and what data would resolve it>
```

Keep entries evidence-first. One day is a tiny sample — most same-day hypotheses cap at `Low` until a
replay or historical comparison raises them.
