"""S2b bot — ALL-DAYS A/B variant.

Byte-identical to the validated Monday-only bot (bot.app.run_s2b) EXCEPT the single A/B
variable: it enters every weekday (Mon-Fri) and allows up to 3 concurrent positions.
Run it on a SEPARATE account and compare to the Monday-only bot to settle, live, whether
trading all days helps or dilutes (the backtests said it dilutes PF and fails 2x-cost stress).

  TRADIER_TOKEN=... TRADIER_ACCOUNT_ID=VA... python -m bot.app.run_s2b_alldays --ticks 1
"""
from bot.app.run_s2b import parse_args, build_and_run


def main(argv=None):
    args = parse_args(argv)
    # BOT B: same core, all weekdays, up to 3 concurrent positions, up to 3 entries/day.
    return build_and_run(args.ticks, args.poll_seconds,
                         entry_days=frozenset({0, 1, 2, 3, 4}), max_open=3, label="ALL-DAYS",
                         shared_account=args.shared_account, max_entries_per_day=3)


if __name__ == "__main__":
    main()
