"""S2b bot — LIVE small-account variant (real money), ALL-DAYS.

Same S2b bull-put-spread core as Bot B (all-days), geometry + sizing calibrated for a tiny
real-money account (~$400) that cannot margin a standard $10-wide spread:
  - NARROW WING ($1): max loss ~$85/contract instead of ~$863, so a contract fits at the lowest
    feasible per-trade risk (~21%).
  - ALL weekdays (Mon-Fri), up to 3 concurrent positions. ($402 buying power / ~$85 margin per
    spread caps the feasible book at ~4; 3 keeps a reserve. Bot B's 5 won't fit a $400 account.)
  - risk caps KEPT INTACT; base_risk_pct is raised only so the indivisible 1-contract minimum
    (already ~21% of a $400 account) isn't auto-blocked. Every structural guard still applies:
    total-risk cap, max_concurrent, settled-cash, ATR cushion, daily-loss halt.

TWO HONEST CAVEATS: (1) a $1 wing leaves almost no room between the 2x stop and max loss, so each
trade is closer to binary; (2) our own research found the ALL-DAYS schedule DILUTES S2b's edge vs
Monday-only (it failed the 2x-cost stress). This is a higher-variance cousin of the validated edge.

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
    # LIVE: all weekdays, up to 3 concurrent, narrow wing, small-account sizing. Caps intact.
    return build_and_run(args.ticks, args.poll_seconds,
                         entry_days=frozenset({0, 1, 2, 3, 4}), max_open=3, label="LIVE",
                         shared_account=args.shared_account, max_entries_per_day=3,
                         s2b_cfg=S2bConfig(wing_width=WING_WIDTH), base_risk_pct=BASE_RISK_PCT)


if __name__ == "__main__":
    main()
