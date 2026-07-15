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
    max_gap_stress_loss_pct: float = 0.06
    expected_stop_slippage: float = 0.10
    daily_pnl_halt_pct: float = 0.02
    # §10 (Priority-0 fix item 5) Black-Scholes shock-grid gap-stress reprice. Replaces the old
    # intrinsic-value-only close estimate (which ignored time value/IV expansion/skew/gamma and so
    # UNDERSTATED the stressed loss). The stress worst-cell is taken over an IV-shock x skew grid.
    risk_free_rate: float = 0.04
    gap_iv_shocks: tuple = (0.03, 0.05, 0.10)   # absolute IV bumps applied to BOTH legs
    gap_skew_bump: float = 0.03                  # extra IV bump on the SHORT leg (stressed put skew)
    gap_stress_widen: float = 0.25               # bid/ask widening haircut on the stressed close
    gap_stress_model: str = "bs"                 # "bs" -> Black-Scholes grid; anything else -> intrinsic
    gap_fallback_iv: float = 0.20                # usable IV when no per-leg IV is available
