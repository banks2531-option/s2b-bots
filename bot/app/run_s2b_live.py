"""S2b bot — LIVE small-account variant (real money).

Same validated Monday SPY bull-put-spread core as Bot A, but geometry + sizing calibrated for a
tiny real-money account (~$400) that cannot margin a standard $10-wide spread:
  - NARROW WING ($1): max loss ~$80/contract instead of ~$863, so one contract fits the account
    at the lowest feasible per-trade risk (~20%).
  - one position at a time (max_open=1), Monday-only entry (the validated edge).
  - risk caps KEPT INTACT; base_risk_pct is raised only so the indivisible 1-contract minimum
    (already ~20% of a $400 account) isn't auto-blocked. Every structural guard still applies:
    total-risk cap, max_concurrent, settled-cash, ATR cushion, daily-loss halt.

This is a HIGHER-VARIANCE COUSIN of the validated edge, not the edge itself: a $1 wing leaves almost
no room between the 2x stop and max loss, so the trade is closer to binary. Use deliberately.

Requires (C8: config from environment only):
  TRADIER_TOKEN, TRADIER_ACCOUNT_ID, TRADIER_BASE_URL=https://api.tradier.com/v1,
  I_UNDERSTAND_THIS_IS_LIVE=yes   (the bot refuses live mode without this explicit flag)

  # sandbox dry-run first:
  TRADIER_BASE_URL=https://sandbox.tradier.com/v1 TRADIER_TOKEN=... TRADIER_ACCOUNT_ID=VA... \
    python -m bot.app.run_s2b_live --ticks 1
"""
from bot.app.run_s2b import parse_args, build_and_run
from bot.strategy.s2b import S2bConfig

WING_WIDTH = 1          # $ wide; fits a ~$400 account at lowest feasible risk (max loss ~$80 vs ~$863)
BASE_RISK_PCT = 0.25    # one $1-wide spread is ~20% of $400; cap must permit the indivisible minimum


def main(argv=None):
    args = parse_args(argv)
    # LIVE: Monday-only, single position, narrow wing, small-account sizing. Caps intact.
    return build_and_run(args.ticks, args.poll_seconds,
                         entry_days=frozenset({0}), max_open=1, label="LIVE",
                         shared_account=args.shared_account, max_entries_per_day=1,
                         s2b_cfg=S2bConfig(wing_width=WING_WIDTH), base_risk_pct=BASE_RISK_PCT)


if __name__ == "__main__":
    main()
