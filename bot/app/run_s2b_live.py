"""S2b bot — LIVE small-account variant (real money), ALL-DAYS.

Same S2b bull-put-spread core as Bot B (all-days), geometry + sizing calibrated for a small
real-money account (~$842, account 6YB71948):
  - $5 WING (widened from $1 on 2026-07-16): max loss ~$425/contract. The $1 wing collected
    ~$0.17, whose 50% take-profit (~$8.50) did not clear ~4x the round-trip cost (~$34.40), so
    transaction_cost_gate rejected nearly every trade. See the WING_WIDTH note below.
  - ONE CONTRACT MAXIMUM (max_entry_qty=1), one open position, one entry per day. This is a
    CONTROLLED LIVE PILOT, not a production sizing.
  - ALL weekdays (Mon-Fri). risk caps KEPT INTACT; base_risk_pct is raised only so the
    indivisible 1-contract minimum isn't auto-blocked. Every structural guard still applies:
    total-risk cap, max_concurrent, settled-cash, ATR cushion, daily-loss halt.

RISK CLASSIFICATION: AGGRESSIVE_LIVE_PILOT. One $5 wing's ~$425 structural loss is ~50% of a
$842 account. This is NOT a low-risk production strategy and must not be described as one. The
advisor's reasonably-controlled band (8.5%-10% of equity per spread) needs ~$4,250-$5,000; the
minimum at which one $5 wing is merely aggressive rather than existential is ~$2,000.

THREE HONEST CAVEATS:
  (1) a single adverse gap can take ~half the account;
  (2) our own research found the ALL-DAYS schedule DILUTES S2b's edge vs Monday-only (it failed
      the 2x-cost stress) -- this is a higher-variance cousin of the validated edge;
  (3) actual_fill_accounting is OFF. Recorded economics are SYNTHETIC (requested credit/qty)
      until leg-level normalization is validated against five live executions. See
      bot/broker/spread_fill.py.

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
    # NOTE ON NAMING: these two flags govern the T11 ALTERNATE $5-wide FALLBACK path only --
    # the branch that asks "what would a $5 wing at this short strike have done?" when a
    # $10-wide candidate is reduced to zero by a risk cap. They do NOT disable Bot C's PRIMARY
    # spread geometry, which is already $5 wide (WING_WIDTH = 5 above). T11 shipped in 6ede08d;
    # "not built" was stale.
    enable_five_wide_live=False,        # the T11 fallback may never trade live (startup-asserted)
    enable_five_wide_shadow=False,      # Bot B carries the shadow evaluation; Bot C does not
    log_intrinsic_gap_comparison=True,  # Step 2B: record what the OLD intrinsic model would have
                                         # permitted beside the enforced Black-Scholes number. This
                                         # is how the fill-rate question gets answered with data
                                         # rather than argument, on the account that is actually
                                         # trading.
    # Advisor nextsteps2 section 2. The advisor's recommendation is 2000; the operator elected on
    # 2026-07-19 to set the gate at the account's existing implicit floor (~$540, where the
    # RiskGate already rejects a $5 wing at base_risk_pct=0.80) so the live pilot keeps producing
    # data at ~$842. RAISE THIS AS THE ACCOUNT FUNDS: 2000 is the point below which one spread is
    # an outsized bet; 4500 brings a single spread's structural loss to the ~10% the risk
    # framework targets. NOTE: not yet enforced -- see the field's note in bot/features.py.
    min_equity_to_open=540.0,
    risk_classification="AGGRESSIVE_LIVE_PILOT",
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
    # CONTROLLED LIVE PILOT (advisor nextsteps2 section 2): one open position, one entry per day.
    # Previously 3/3, inherited from the $1-wing era when three ~$85 spreads fit a $400 account.
    # A $5 wing is ~50% of this account, so three is not a reachable state -- the aggregate risk
    # budget already blocks the second. Declaring 1/1 makes the intended ceiling match the
    # enforced one instead of relying on a risk cap to contradict the wrapper.
    return build_and_run(args.ticks, args.poll_seconds,
                         entry_days=frozenset({0, 1, 2, 3, 4}), max_open=1, label="LIVE",
                         shared_account=args.shared_account, max_entries_per_day=1,
                         s2b_cfg=S2bConfig(wing_width=WING_WIDTH), base_risk_pct=BASE_RISK_PCT,
                         features=LIVE_FEATURES)


if __name__ == "__main__":
    main()
