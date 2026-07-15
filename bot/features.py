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
    est_commission_per_leg_rt: float = 0.65
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
    max_gap_stress_loss_pct: float = 0.06
    expected_stop_slippage: float = 0.10
    daily_pnl_halt_pct: float = 0.02
