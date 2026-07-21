"""Feature flags + thresholds for the all-days S2b bot (partner review v2 §1, §2)."""
from dataclasses import dataclass

@dataclass
class S2bFeatures:
    # §1 feature flags
    credit_tiers: bool = False
    transaction_cost_gate: bool = False
    entry_price_ladder: bool = False
    tp_price_ladder: bool = False
    aggregate_risk_budget: bool = False
    actual_fill_accounting: bool = False
    regime_shadow_monitor: bool = False
    markout_tracking: bool = False   # §14 (Priority-0 fix item 4): research entry-markout collector.
                                      # SEPARATE flag from regime_shadow_monitor so the §14 markout
                                      # path (record + batched resolve) can be toggled independently
                                      # of the shadow monitor. LOG ONLY; left OFF in the deployed
                                      # all-days config and always OFF for Bot C (byte-identical).
    decision_logging: bool = False   # §1/§12: write a DECISION record (reason+flags+telemetry) per entry-cycle return
    # explicitly-off Phase-4 alpha (never enabled in this program)
    regime_entry_blocks: bool = False
    automatic_hedging: bool = False
    bearish_module: bool = False
    early_loss_exit: bool = False
    # §2 allocated equity
    allocated_equity: float = 72000.0
    # §3 credit tiers
    min_credit_ratio: float = 0.10
    full_size_credit_floor: float = 0.115
    full_size_credit_ceiling: float = 0.14
    probe_size_multiplier: float = 0.40
    credit_adapt_min_signals: int = 30
    # §4 expected executable credit + quote guards
    expected_entry_slippage: float = 0.03
    max_quote_age_seconds: float = 2.0
    max_package_width_ratio: float = 0.25
    max_signal_to_order_spot_move_atr: float = 0.10
    # §5 cost gate
    take_profit_percent: float = 0.50
    min_target_to_cost_ratio: float = 4.0
    commission_per_contract_per_leg_per_side: float = 0.65
    # §6/§7 ladders
    entry_reprice_seconds: float = 5.0
    entry_reprice_increment: float = 0.01
    entry_max_work_seconds: float = 30.0
    # §9/§10/§11 risk budgets
    max_entry_stop_risk_pct: float = 0.0075
    max_same_day_stop_risk_pct: float = 0.02
    max_expiry_stop_risk_pct: float = 0.03
    max_total_stop_risk_pct: float = 0.04
    max_total_structural_risk_pct: float = 0.15
    max_trade_structural_risk_pct: float = 0.05   # per-trade structural cap used by size_qty (§9)
    max_gap_stress_loss_pct: float = 0.06        # 1.5-ATR gap-stress budget (spec §8, unchanged)
    # Per-scenario gap-stress budgets for the incremental gap quantity model (spec §8/§9): the
    # 1.0-ATR move is a shallower shock -> a LOOSER limit; the 2.0-ATR move is the deepest -> a
    # TIGHTER limit. The 1.5-ATR scenario keeps using max_gap_stress_loss_pct above.
    gap_1atr_limit_pct: float = 0.10
    gap_2atr_limit_pct: float = 0.04
    expected_stop_slippage: float = 0.10
    daily_pnl_halt_pct: float = 0.02
    # §10 (Priority-0 fix item 5) Black-Scholes shock-grid gap-stress reprice. Replaces the old
    # intrinsic-value-only close estimate (which ignored time value/IV expansion/skew/gamma and so
    # UNDERSTATED the stressed loss). The stress worst-cell is taken over an IV-shock x skew grid.
    risk_free_rate: float = 0.04
    gap_iv_shocks: tuple = (0.03, 0.05, 0.10)   # absolute IV bumps applied to BOTH legs
    gap_skew_bump: float = 0.03                  # extra IV bump on the SHORT leg (stressed put skew)
    gap_stress_widen: float = 0.25               # bid/ask widening haircut on the stressed close
    # Advisor Step 2A: the old `gap_stress_model` flag was RETIRED. It read as though it chose the
    # enforcement model, but post-v2 refinement §9 ("do not use intrinsic value alone") made gap
    # enforcement unconditionally Black-Scholes -- so setting it to "intrinsic" silently gave you BS
    # enforcement with understating logs. A setting that appears to control risk but does not is worse
    # than no setting. It is replaced by an explicit pair:
    gap_stress_enforcement_model: str = "black_scholes"   # validated at startup; see
                                                           # validate_gap_stress_config below
    log_intrinsic_gap_comparison: bool = False   # Step 2B: also compute the INTRINSIC numbers and log
                                                  # them beside the enforced BS ones. Comparison only --
                                                  # never enforced. ON for Bot B during validation,
                                                  # off by default so no other bot pays the cost.
    gap_fallback_iv: float = 0.20                # usable IV when no per-leg IV is available
    # ── Bot C controlled-deployment safeguards (advisor directive botc.txt, 2026-07-19) ──────────
    max_entry_qty: int = None            # hard ceiling on contracts per entry, applied as one more
                                          # QuantityCap (min of all caps) so it can only ever REDUCE
                                          # a risk-approved quantity, never raise a zero. None = no
                                          # ceiling (Bot B, which is validating full sizing).
    enable_low_credit_08_to_10: bool = True   # the 0.08-0.10 low_credit_safety exception lane. Code
                                               # complete and unit-tested, but its inputs depend on
                                               # live feed fields not yet verified against real
                                               # broker responses -- so OFF for the first live
                                               # release. Turning it off does NOT affect the regular
                                               # 10%+ probe and full-size lanes.
    enable_five_wide_live: bool = False   # $5-wing TRADING. Stays False until T11 ships; the shadow
                                           # evaluation is a separate, non-trading flag.
    enable_five_wide_shadow: bool = False  # T11 not built yet
    enable_alternate_expirations: bool = False   # T8 (spec §14): evaluate up to N expirations and
                                                  # pick the best by expected NET target profit.
                                                  # OFF by default so the live path stays unchanged;
                                                  # ON for Bot B sandbox validation.
    alternate_expiration_count: int = 3
    alternate_expiration_min_dte: int = 4
    entry_state_tracking: bool = False   # T9 (spec §16): emit an ENTRY_STATE record on each state
                                          # TRANSITION. Observability only -- changes no order
                                          # decision. Off by default; on for Bot B.

    # ── Advisor nextsteps2 section 2: explicit capital gate ──────────────────────────────────
    # ENFORCED as of Task 7b (2026-07-19) in run_entry_cycle, which blocks new entries below this
    # floor. The check reads risk_equity() -- min(allocated_equity, broker_equity) -- so a bot
    # allocated a slice of a shared account is gated on its allocation, not a co-occupant's
    # capital. Behaviour is pinned by bot/tests/test_min_equity_gate.py.
    min_equity_to_open: float = 0.0   # block NEW entries below this account equity. 0.0 = no gate
                                       # (Bot B sandbox). Today Bot C's floor is IMPLICIT -- it
                                       # emerges from base_risk_pct=0.80 against a ~$425 max loss,
                                       # so it moves silently whenever either is retuned. Making it
                                       # explicit means the floor is a stated number that can be
                                       # raised as the account funds, not an accident of two other
                                       # settings. The advisor recommends 2000 (below which one $5
                                       # wing is >21% of equity) and 4500 for normal operation;
                                       # Bot C runs 540 by operator decision so the live pilot can
                                       # keep collecting data at its current ~$842.
    risk_classification: str = "UNCLASSIFIED"   # honest label on the config itself.
                                                 # "AGGRESSIVE_LIVE_PILOT" for Bot C: one $5 wing
                                                 # is ~50% of its account. The advisor's point is
                                                 # that no software setting makes that "low risk",
                                                 # so the config should not imply otherwise.
                                                 # NOT read by anything -- it is documentation that
                                                 # lives with the values it describes, so it cannot
                                                 # drift out of sync the way a comment in a wrapper
                                                 # can. Deliberately NOT wired into _log_decision:
                                                 # that path enumerates _DECISION_LOG_FIELDS, and a
                                                 # header/field mismatch there is what previously
                                                 # made every telemetry row unparseable.


class ConfigurationError(Exception):
    """Raised at startup when a configuration value would put the bot in an unsafe or
    self-contradictory state. Fatal by design: better to refuse to start than to trade on a
    misunderstood risk setting."""


def validate_gap_stress_config(f) -> None:
    """Advisor Step 2A: fail fast at startup unless gap-stress enforcement is Black-Scholes.

    Spec §9 forbids enforcing gap stress on intrinsic value alone -- the intrinsic model ignores time
    value, IV expansion, skew and gamma, and so materially UNDERSTATES the stressed loss. There is no
    supported production configuration in which anything else enforces, so this is a hard error
    rather than a warning. Also rejects the RETIRED `gap_stress_model` flag outright: a leftover in a
    config file would otherwise sit there looking meaningful while controlling nothing."""
    if getattr(f, "gap_stress_enforcement_model", None) != "black_scholes":
        raise ConfigurationError(
            "Production gap-stress enforcement must use Black-Scholes; got "
            f"{getattr(f, 'gap_stress_enforcement_model', None)!r}.")
    if hasattr(f, "gap_stress_model"):
        raise ConfigurationError(
            "`gap_stress_model` was retired (advisor Step 2A): it appeared to select the gap-stress "
            "ENFORCEMENT model but only ever affected logging. Use `gap_stress_enforcement_model` "
            "(enforcement, must be 'black_scholes') and `log_intrinsic_gap_comparison` (logging).")


def validate_live_config(f, *, live: bool, expected_structural_pct: float = None) -> None:
    """Advisor directive (botc.txt) pre-deployment assertions, checked at startup so a
    misconfiguration refuses to start rather than trading real money on a misunderstood setting.

    `live=False` (Bot B sandbox) skips the live-only ceiling: Bot B exists to validate FULL sizing,
    so inheriting Bot C's one-contract cap would defeat its purpose.

    DEVIATION, deliberate: the directive says `assert MAX_TOTAL_STRUCTURAL_RISK_PCT == 0.15`. Bot C
    does not run 0.15 -- it runs 0.50, set on 2026-07-17 because a single $5-wing spread is ~49% of
    its ~$842 account and the big-account 0.15 default sized every trade to zero. Asserting 0.15
    would refuse to start Bot C. The directive's INTENT is "this deployment must not loosen risk
    limits", so the caller passes the value Bot C already runs and any drift from it fails."""
    validate_gap_stress_config(f)
    # Advisor checklist: pin the credit floors at STARTUP, not only in the test suite. The droplet
    # runs no tests, so a floor edited directly on the server would otherwise go uncaught. Local
    # import keeps features.py free of a module-load dependency on credit_quality. Applies to BOTH
    # bots -- the floors are shared and this deployment must not loosen them.
    from bot.strategy.credit_quality import (ABSOLUTE_CREDIT_FLOOR, STANDARD_PROBE_FLOOR,
                                             FULL_SIZE_CREDIT_FLOOR)
    if (ABSOLUTE_CREDIT_FLOOR, STANDARD_PROBE_FLOOR, FULL_SIZE_CREDIT_FLOOR) != (0.08, 0.10, 0.115):
        raise ConfigurationError(
            "a credit floor drifted from the advisor-pinned values "
            f"(absolute={ABSOLUTE_CREDIT_FLOOR}, probe={STANDARD_PROBE_FLOOR}, "
            f"full_size={FULL_SIZE_CREDIT_FLOOR}; expected 0.08 / 0.10 / 0.115). "
            "This deployment must not loosen entry-quality thresholds.")
    if f.bearish_module:
        raise ConfigurationError("bearish module must stay off (advisor directive): bearish_module")
    if getattr(f, "enable_five_wide_live", False):
        raise ConfigurationError("five_wide live trading must stay off until T11 ships: "
                                 "enable_five_wide_live")
    if expected_structural_pct is not None and \
            f.max_total_structural_risk_pct != expected_structural_pct:
        raise ConfigurationError(
            "structural risk limit changed from this bot's deployed calibration: "
            f"max_total_structural_risk_pct={f.max_total_structural_risk_pct}, "
            f"expected {expected_structural_pct}. This deployment must not alter risk limits.")
    if not live:
        return
    if f.max_entry_qty != 1:
        raise ConfigurationError(
            "first live release requires the one-contract ceiling: max_entry_qty must be 1, got "
            f"{f.max_entry_qty!r}")
    if getattr(f, "enable_low_credit_08_to_10", False):
        raise ConfigurationError(
            "the 0.08-0.10 low_credit lane must stay off in live until its quote-age and "
            "expected-move inputs are verified against real broker responses: "
            "enable_low_credit_08_to_10")
