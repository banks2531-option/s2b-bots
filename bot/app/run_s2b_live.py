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
from bot.features import S2bFeatures

# WING WIDENED $1 -> $5 on 2026-07-16 (account funding to $600-800). RATIONALE: a $1 wing collects
# ~$0.17 credit, whose 50% take-profit target (~$8.50) does NOT clear ~4x the round-trip cost (~$34.40),
# so transaction_cost_gate rejects nearly every $1-wing trade (negative post-cost edge). A $5 wing
# collects ~$0.65-0.85, clearing the cost gate. TRADE-OFF ACCEPTED BY OPERATOR: a $5 wing's max loss
# (~$425/contract) is ~71% of a $600 account, far above the ~25% the risk framework targets -- so this
# is a HIGH-VARIANCE, one-bad-gap-hurts config. The strategy's sane-risk home is a ~$2,000 account; this
# is a deliberate override to run it on $600-800.
WING_WIDTH = 5
# base_risk_pct raised 0.25 -> 0.80. It feeds RiskGate.max_risk_pct (wiring.py), the per-trade cap:
# a $5-wing's max loss (~$431/contract) is ~72% of a $600 account, so the cap must be >= ~0.72 or the
# RiskGate rejects it. 0.80 gives a small buffer (trades down to ~$539 equity, so a minor drawdown at
# $600 doesn't halt it) WITHOUT raising the actual per-trade risk (still ~72%, fixed by the wing; sizing
# is always 1 contract). SELF-GATES until funded: at the current ~$362 the $5-wing is 119% of equity, so
# the RiskGate rejects every entry -- the bot places NO trades until the account is funded to ~$540+.
BASE_RISK_PCT = 0.80

# LIVE feature set (partner review v2). Enabled 2026-07-16; aggregate_risk_budget DISABLED 2026-07-16
# as part of the forced $5-wing/$600 override. The protective ENTRY-QUALITY screens (transaction_cost_gate
# + credit_tiers) stay ON -- they're the whole point of widening the wing (avoid thin-credit blowups).
# accounting + logging stay ON. The two ORDER-SUBMISSION LADDERS (entry_price_ladder, tp_price_ladder)
# stay OFF -- unvalidated on a real broker. Phase-4 alpha flags stay OFF.
# WHY aggregate_risk_budget IS OFF: its caps are small % of equity (total-stop 4%, gap-stress 6%); on a
# $600 account those are ~$24/$36, but one $5-wing contract carries ~$150 stop / ~$400 gap risk, so the
# budget would size EVERY trade to 0 (correctly -- it's a small-account safety). Forcing the trade means
# disabling it and reverting to simple base_risk_pct sizing. PROTECTION LOST: gap-stress test, continuous
# daily-risk gate, and daily-loss halt. STILL ACTIVE: cost gate, credit tiers, base RiskGate caps, stops.
LIVE_FEATURES = S2bFeatures(
    credit_tiers=True,
    transaction_cost_gate=True,
    aggregate_risk_budget=False,   # DISABLED for the forced $5-wing/$600 override (see note above)
    # actual_fill_accounting DISABLED 2026-07-17: it is broken against the LIVE Tradier broker (two
    # real-money quirks the sandbox doesn't have). (1) A credit spread's order-level avg_fill_price is
    # reported NEGATIVE (e.g. -0.92 = $0.92 received), so recording credit=avg_fill_price flipped the
    # sign -> negative credit -> phantom STOP + failed-close halt + corrupted P&L. (2) The order-level
    # exec_quantity counts LEGS (2) not CONTRACTS (1), doubling the recorded qty. With the flag OFF the
    # bot records the REQUESTED credit/qty (correct). Re-enabling requires fixing _to_execution_result
    # to negate the credit sign and read leg-level (not order-level) exec_quantity, then re-validating
    # on the real broker.
    actual_fill_accounting=False,
    regime_shadow_monitor=True,
    markout_tracking=True,
    decision_logging=True,
    # entry_price_ladder / tp_price_ladder: HELD OFF -- unvalidated on a real broker (real-money guard).
)


def main(argv=None):
    args = parse_args(argv)
    # LIVE: all weekdays, up to 3 concurrent, narrow wing, small-account sizing. Caps intact.
    return build_and_run(args.ticks, args.poll_seconds,
                         entry_days=frozenset({0, 1, 2, 3, 4}), max_open=3, label="LIVE",
                         shared_account=args.shared_account, max_entries_per_day=3,
                         s2b_cfg=S2bConfig(wing_width=WING_WIDTH), base_risk_pct=BASE_RISK_PCT,
                         features=LIVE_FEATURES)


if __name__ == "__main__":
    main()
