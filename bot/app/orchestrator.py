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


def run_management_cycle(state: BotState, deps: Deps, today: str) -> tuple:
    results = monitor_positions(
        state.open_positions,
        mark_fn=deps.mark_position,
        dte_fn=lambda p: deps.dte_of(p, today),
        close_fn=deps.close_spread,
        cfg=deps.manage_cfg,
    )
    alerts = alerts_for_cycle(results, drift_report=None)
    if alerts:
        deps.alert_sink(alerts)
    # remove only positions whose close actually filled
    closed_ok = {id(r.position) for r in results if not r.failed}
    state.open_positions = [p for p in state.open_positions if id(p) not in closed_ok]
    if should_halt_new_entries(alerts):
        state.halted = True
        state.halt_reason = "failed close"
    return state, results


def run_entry_cycle(state: BotState, deps: Deps, now) -> tuple:
    today = now.strftime("%Y-%m-%d")
    if state.halted or not is_entry_day(today) or now.hour < 10 or state.open_positions:
        return state, None
    spot = deps.get_spot("SPY")
    atr = deps.get_atr("SPY")
    expiry = deps.pick_expiry(today)
    order = build_spread_order(spot, atr, deps.get_chain("SPY", expiry), deps.s2b_cfg)
    if order is None:
        return state, "no_order"
    pct_rank, change = deps.get_vix_regime()
    risk = regime_adjusted_risk_pct(deps.base_risk_pct, pct_rank, change)
    order.qty = contracts_for_risk(deps.account_equity, order.max_loss_per_contract, risk)
    acct = deps.account_state(today, len(state.open_positions))
    decision = RiskGate(deps.risk_cfg).is_order_allowed(order, acct)
    if not decision.allowed:
        return state, decision.reason
    status = deps.open_spread(to_tradier_payload(order, expiry, order.qty))
    if status == "filled":
        state.open_positions.append(ManagedPosition(
            "SPY", order.short_strike, order.long_strike, order.credit, order.qty, expiry))
    return state, status
