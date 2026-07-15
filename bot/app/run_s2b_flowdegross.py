"""S2b bot — FLOW-DEGROSS variant (opt-in flow-flip de-gross).

Identical to the all-days bot (bot.app.run_s2b_alldays): enters every weekday (Mon-Fri), up to 5
concurrent positions, up to 5 entries/day. The ONE added behavior is the backtested flow-degross:
when market-wide options flow flips from BULLISH to BEARISH across ticks, CLOSE any position that is
BOTH opened the same day AND not yet profitable (debit-to-close >= entry credit). Profitable or
prior-day positions are left to normal management. Runs on its OWN log/state files
(trades_flowdegross.csv / state_flowdegross.json), so it never touches the Monday-only or live bots.

  TRADIER_TOKEN=... TRADIER_ACCOUNT_ID=VA... python -m bot.app.run_s2b_flowdegross --ticks 1
"""
from bot.app.run_s2b import parse_args, build_and_run


def main(argv=None):
    args = parse_args(argv)
    # All weekdays, up to 5 concurrent / 5 entries per day (like Bot B), PLUS opt-in flow-flip de-gross.
    return build_and_run(args.ticks, args.poll_seconds,
                         entry_days=frozenset({0, 1, 2, 3, 4}), max_open=5, label="FLOWDEGROSS",
                         shared_account=args.shared_account, max_entries_per_day=5,
                         degross_on_flow_flip=True)


if __name__ == "__main__":
    main()
