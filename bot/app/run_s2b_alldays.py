"""S2b bot — ALL-DAYS A/B variant.

Byte-identical to the validated Monday-only bot (bot.app.run_s2b) EXCEPT the single A/B
variable: it enters every weekday (Mon-Fri) and allows up to 5 concurrent positions.
Run it on a SEPARATE account and compare to the Monday-only bot to settle, live, whether
trading all days helps or dilutes (the backtests said it dilutes PF and fails 2x-cost stress).

  TRADIER_TOKEN=... TRADIER_ACCOUNT_ID=VA... python -m bot.app.run_s2b_alldays --ticks 1
"""
from bot.app.run_s2b import parse_args, build_and_run


def main(argv=None):
    args = parse_args(argv)
    # BOT B: same core, all weekdays, up to 5 concurrent positions, up to 5 entries/day.
    # At base_risk_pct 10%/trade, a full 5-book is ~50% of equity at risk (build_deps derives the
    # risk-gate concurrency + total-risk caps from max_open so the gate permits the full book).
    return build_and_run(args.ticks, args.poll_seconds,
                         entry_days=frozenset({0, 1, 2, 3, 4}), max_open=5, label="ALL-DAYS",
                         shared_account=args.shared_account, max_entries_per_day=5)


if __name__ == "__main__":
    main()
