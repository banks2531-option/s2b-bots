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
from bot.features import S2bFeatures, validate_live_config

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

# LIVE feature set (partner review v2). Enabled 2026-07-16. aggregate_risk_budget was DISABLED
# 2026-07-16 (its big-account default caps sized every $5-wing trade to 0), then RE-ENABLED 2026-07-17
# with caps RE-CALIBRATED to the small (~$842) account (below). What it restores: a book-CONCENTRATION
# cap (hold at most ~1 $5-wing at a time -- blocks stacking a 2nd ~49%-of-account bet) and a DAILY-LOSS
# HALT (~one bad trade). What it does NOT do (analysis 2026-07-17): "sit out volatile days" for a single
# $5-wing -- the -1.5 ATR gap-stress on a narrow $5 wing saturates at ~max loss (~$410) in ALL conditions
# (short is ~1 ATR OTM), so a single new $5-wing always looks like ~49% loss regardless of VIX. The caps
# are therefore SET so 1 $5-wing passes from flat and a 2nd is blocked -- concentration + drawdown control,
# not a volatility filter. Caps are % of risk_equity = min(allocated_equity 72000, broker_equity ~$842).
# The ENTRY-QUALITY screens (transaction_cost_gate + credit_tiers), accounting/logging stay ON; the two
# ORDER-SUBMISSION LADDERS stay OFF (unvalidated on a real broker); Phase-4 alpha flags stay OFF.
# actual_fill_accounting stays OFF: broken vs the LIVE broker (negative credit sign + leg-count qty);
# re-enabling needs _to_execution_result fixed (negate credit sign, read leg-level exec_quantity) + live
# re-validation.
LIVE_FEATURES = S2bFeatures(
    credit_tiers=True,
    transaction_cost_gate=True,
    actual_fill_accounting=False,   # broken vs LIVE broker (sign + leg-count); see note above
    regime_shadow_monitor=True,
    markout_tracking=True,
    decision_logging=True,
    # aggregate_risk_budget RE-ENABLED 2026-07-17 with small-account-calibrated caps (defaults in
    # parentheses). These are much larger % than the big-account defaults because a single $5-wing is
    # inherently ~half this account; they permit exactly ~1 $5-wing and block stacking + halt after a
    # bad day.
    aggregate_risk_budget=True,
    max_gap_stress_loss_pct=0.50,       # (0.06) ~$421: permits 1 $5-wing (~$410 stress) from flat, blocks a 2nd
    daily_pnl_halt_pct=0.25,            # (0.02) ~$210: halt new entries after ~one bad trade
    max_total_stop_risk_pct=0.30,       # (0.04) ~$253: total book stop risk ~1 position
    max_entry_stop_risk_pct=0.25,       # (0.0075) ~$210: sizes the ~$190-stop $5-wing to 1 contract
    max_same_day_stop_risk_pct=0.25,    # (0.02) ~1 entry/day
    max_expiry_stop_risk_pct=0.30,      # (0.03) ~1 position per expiry
    # STRUCTURAL caps (max-loss based, used by size_qty + cap_to_budgets). MUST be >= the $5-wing's
    # ~49%-of-account max loss or size_qty returns 0 -> no trade. Set to permit 1 $5-wing, block a 2nd.
    max_trade_structural_risk_pct=0.50, # (0.05) ~$421 >= one $5-wing structural (~$410) -> sizes 1
    max_total_structural_risk_pct=0.50, # (0.15) ~$421: total book structural ~1 $5-wing, blocks a 2nd
    # entry_price_ladder / tp_price_ladder: HELD OFF -- unvalidated on a real broker (real-money guard).
    #
    # ── post-v2 refinement controlled release (advisor directive botc.txt, 2026-07-19) ───────────
    # The completed T1-T7 work ships to Bot C now rather than waiting five sandbox sessions, but
    # under a hard ceiling so an undiscovered fault costs one contract, not a full-size position.
    max_entry_qty=1,                    # applied as a QuantityCap in the min() -- can only REDUCE a
                                         # risk-approved quantity, never raise a zero into a trade.
                                         # REMOVE only after the directive's exit criteria are met:
                                         # >=3 processed live entry decisions, >=1 actual fill, >=1
                                         # risk-based reduction or validated full-size decision, no
                                         # duplicate orders, no partial-fill mismatch, no
                                         # broker/local divergence, stable gap numbers across cycles,
                                         # working restart persistence.
    enable_low_credit_08_to_10=False,   # the 0.08-0.10 exception lane stays OFF live. It is code
                                         # complete and unit-tested, but its inputs read Tradier
                                         # bid_date/ask_date and greeks.mid_iv, which have NOT been
                                         # verified against real broker responses -- only synthetic
                                         # fixtures. The regular 10%+ probe and full-size lanes are
                                         # unaffected. Turn on once the fields are confirmed live.
    enable_five_wide_live=False,        # T11 not built; no live $5-wing trading
    enable_five_wide_shadow=False,      # T11 not built
    log_intrinsic_gap_comparison=True,  # Step 2B: record what the OLD intrinsic model would have
                                         # permitted beside the enforced Black-Scholes number. This
                                         # is how the fill-rate question gets answered with data
                                         # rather than argument, on the account that is actually
                                         # trading.
)

# The structural-risk limit Bot C is CURRENTLY calibrated to run (set 2026-07-17 for the ~$842
# account: a single $5-wing is ~49% of it, so the big-account 0.15 default sized every trade to
# zero). validate_live_config asserts the deployed config still matches this, so the post-v2
# deployment cannot quietly alter a risk limit. NOTE: the advisor's checklist says
# `assert MAX_TOTAL_STRUCTURAL_RISK_PCT == 0.15` -- that is the big-account default and asserting it
# literally would refuse to start Bot C. The intent (do not loosen limits in this deployment) is
# enforced against Bot C's own calibration instead.
BOT_C_STRUCTURAL_PCT = 0.50


def main(argv=None):
    args = parse_args(argv)
    # Advisor directive (botc.txt): startup assertions. Fatal by design -- on a real-money account,
    # refusing to start beats trading on a misunderstood risk setting.
    validate_live_config(LIVE_FEATURES, live=True,
                         expected_structural_pct=BOT_C_STRUCTURAL_PCT)
    # LIVE: all weekdays, up to 3 concurrent, narrow wing, small-account sizing. Caps intact.
    return build_and_run(args.ticks, args.poll_seconds,
                         entry_days=frozenset({0, 1, 2, 3, 4}), max_open=3, label="LIVE",
                         shared_account=args.shared_account, max_entries_per_day=3,
                         s2b_cfg=S2bConfig(wing_width=WING_WIDTH), base_risk_pct=BASE_RISK_PCT,
                         features=LIVE_FEATURES)


if __name__ == "__main__":
    main()
