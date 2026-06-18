"""Bot orchestrator: tick = reconcile -> manage -> enter, with halt-gating (spec §3,4,5,7)."""
from dataclasses import dataclass, field

from bot.sizing import contracts_for_risk, regime_adjusted_risk_pct
from bot.risk_gate import RiskGate, RiskConfig
from bot.strategy.s2b import is_entry_day, build_spread_order, S2bConfig, to_tradier_payload
from bot.strategy.manage import (monitor_positions, ManageConfig, ManagedPosition)
from bot.ops.ledger import reconcile
from bot.ops.monitor import alerts_for_cycle, should_halt_new_entries


@dataclass
class BotState:
    open_positions: list = field(default_factory=list)   # list[ManagedPosition]
    halted: bool = False
    halt_reason: str = ""


@dataclass
class Deps:
    get_spot: callable            # (symbol) -> float
    get_atr: callable             # (symbol) -> float
    get_chain: callable           # (symbol, expiry) -> list[OptionQuote]
    pick_expiry: callable         # (today_str) -> expiry_str
    get_vix_regime: callable      # () -> (vix_pct_rank, vix_1d_change)
    account_state: callable       # (today, concurrent) -> AccountState
    mark_position: callable       # (ManagedPosition) -> float (debit to close)
    dte_of: callable              # (ManagedPosition, today) -> int
    open_spread: callable         # (payload) -> status str ("filled" on success)
    close_spread: callable        # (ManagedPosition, action) -> status str
    broker_positions: callable    # () -> list[ManagedPosition]
    broker_equity: callable       # () -> float
    bot_equity: callable          # () -> float
    alert_sink: callable          # (list[Alert]) -> None
    s2b_cfg: object = field(default_factory=S2bConfig)
    manage_cfg: object = field(default_factory=ManageConfig)
    risk_cfg: object = field(default_factory=RiskConfig)
    base_risk_pct: float = 0.10
    account_equity: float = 20_000.0


def run_reconcile_cycle(state: BotState, deps: Deps) -> tuple:
    drift = reconcile(state.open_positions, deps.broker_positions(),
                      deps.bot_equity(), deps.broker_equity())
    alerts = alerts_for_cycle([], drift_report=drift)
    if alerts:
        deps.alert_sink(alerts)
    if should_halt_new_entries(alerts):
        state.halted = True
        state.halt_reason = "reconcile drift"
    return state, drift
