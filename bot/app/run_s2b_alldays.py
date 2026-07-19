"""S2b bot — ALL-DAYS A/B variant.

Byte-identical to the validated Monday-only bot (bot.app.run_s2b) EXCEPT the single A/B
variable: it enters every weekday (Mon-Fri) and allows up to 5 concurrent positions.
Run it on a SEPARATE account and compare to the Monday-only bot to settle, live, whether
trading all days helps or dilutes (the backtests said it dilutes PF and fails 2x-cost stress).

  TRADIER_TOKEN=... TRADIER_ACCOUNT_ID=VA... python -m bot.app.run_s2b_alldays --ticks 1
"""
from bot.app.run_s2b import parse_args, build_and_run
from bot.features import S2bFeatures

# Partner review v2 feature set for Bot B (all-days). The four Phase-4 alpha flags
# (regime_entry_blocks, automatic_hedging, bearish_module, early_loss_exit) stay OFF.
ALLDAYS_FEATURES = S2bFeatures(
    # ── post-v2 refinement, Bot B sandbox validation config (advisor botbnextsteps) ──────────────
    # Bot B is the validation environment: it runs the FULL feature set at FULL sizing so the
    # quantity-cap system can demonstrate whether it correctly selects one, two or more contracts
    # under the real budgets. Deliberately NO max_entry_qty ceiling here -- that is Bot C's
    # controlled-release guard, and imposing it on the sandbox would defeat the point.
    enable_alternate_expirations=True,   # T8: evaluate up to 3 expirations, pick best net profit
    log_intrinsic_gap_comparison=True,   # Step 2B: measure the BS-vs-intrinsic gap difference
    enable_five_wide_shadow=False,       # T11 not built yet; flipped on when it lands
    enable_five_wide_live=False,         # never
    credit_tiers=True,
    transaction_cost_gate=True,
    entry_price_ladder=True,
    tp_price_ladder=True,
    aggregate_risk_budget=True,
    actual_fill_accounting=True,
    regime_shadow_monitor=True,
    # markouts were collected pre-branch via regime_shadow_monitor; the Task 4 flag split moved them
    # to their own flag -> keep them ON for Bot B
    markout_tracking=True,
    decision_logging=True,
)


def main(argv=None):
    args = parse_args(argv)
    # BOT B: same core, all weekdays, up to 5 concurrent positions, up to 5 entries/day.
    # At base_risk_pct 10%/trade, a full 5-book is ~50% of equity at risk (build_deps derives the
    # risk-gate concurrency + total-risk caps from max_open so the gate permits the full book).
    return build_and_run(args.ticks, args.poll_seconds,
                         entry_days=frozenset({0, 1, 2, 3, 4}), max_open=5, label="ALL-DAYS",
                         shared_account=args.shared_account, max_entries_per_day=5,
                         features=ALLDAYS_FEATURES)


if __name__ == "__main__":
    main()
