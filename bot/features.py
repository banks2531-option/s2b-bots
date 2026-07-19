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
