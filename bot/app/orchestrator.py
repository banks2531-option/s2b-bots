"""Bot orchestrator: tick = reconcile -> manage -> enter, with halt-gating (spec §3,4,5,7)."""
from dataclasses import dataclass, field

from bot.sizing import contracts_for_risk, regime_adjusted_risk_pct
from bot.risk_gate import RiskGate, RiskConfig
from bot.strategy.s2b import build_spread_order, S2bConfig, to_tradier_payload
from bot.strategy.manage import (monitor_positions, ManageConfig, ManagedPosition)
from bot.ops.ledger import reconcile, position_key
from bot.ops.monitor import alerts_for_cycle, should_halt_new_entries, Alert, Severity


@dataclass
class BotState:
    """Bot state. NOTE: `halted` is sticky — it persists across ticks until an operator
    explicitly calls clear_halt() after investigating. The bot never self-clears a halt."""
    open_positions: list = field(default_factory=list)   # list[ManagedPosition]
    halted: bool = False
    halt_reason: str = ""
    last_entry_date: str = ""                             # guards one entry per calendar day

    def clear_halt(self):
        self.halted = False
        self.halt_reason = ""


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
    entry_days: frozenset = frozenset({0})   # weekdays allowed to enter (0=Mon). A/B variable.
    max_open: int = 1                        # max concurrent open positions for this bot
    shared_account: bool = False             # True when multiple bots share ONE broker account


def run_reconcile_cycle(state: BotState, deps: Deps) -> tuple:
    try:
        drift = reconcile(state.open_positions, deps.broker_positions(),
                          deps.bot_equity(), deps.broker_equity(),
                          ignore_untracked=deps.shared_account, qty_at_least=deps.shared_account)
    except Exception as exc:  # reconcile failure must NOT prevent management/stops from running
        deps.alert_sink([Alert(Severity.CRITICAL, f"reconcile failed: {exc}")])
        return state, False
    alerts = alerts_for_cycle([], drift_report=drift)
    if alerts:
        deps.alert_sink(alerts)
    if should_halt_new_entries(alerts):
        state.halted = True
        state.halt_reason = "reconcile drift"
    return state, True


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
    closed_ok = {position_key(r.position) for r in results if not r.failed}
    state.open_positions = [p for p in state.open_positions if position_key(p) not in closed_ok]
    if should_halt_new_entries(alerts):
        state.halted = True
        state.halt_reason = "failed close"
    return state, results


def run_entry_cycle(state: BotState, deps: Deps, now) -> tuple:
    today = now.strftime("%Y-%m-%d")
    # `now` MUST be in US/Eastern (the live wiring is responsible for that). Enter only 10:00-15:59 ET.
    # Gates: not halted; today is an allowed entry weekday (the A/B variable); within RTH;
    # under the concurrent-position cap; and not already entered today (one entry per day).
    if (state.halted or now.weekday() not in deps.entry_days or not (10 <= now.hour < 16)
            or len(state.open_positions) >= deps.max_open or state.last_entry_date == today):
        return state, None
    spot = deps.get_spot("SPY")
    atr = deps.get_atr("SPY")
    expiry = deps.pick_expiry(today)
    order = build_spread_order(spot, atr, deps.get_chain("SPY", expiry), deps.s2b_cfg)
    if order is None:
        return state, "no_order"
    acct = deps.account_state(today, len(state.open_positions))
    pct_rank, change = deps.get_vix_regime()
    risk = regime_adjusted_risk_pct(deps.base_risk_pct, pct_rank, change)
    order.qty = contracts_for_risk(acct.equity, order.max_loss_per_contract, risk)
    decision = RiskGate(deps.risk_cfg).is_order_allowed(order, acct)
    if not decision.allowed:
        return state, decision.reason
    status = deps.open_spread(to_tradier_payload(order, expiry, order.qty))
    if status == "filled":
        state.open_positions.append(ManagedPosition(
            "SPY", order.short_strike, order.long_strike, order.credit, order.qty, expiry))
        state.last_entry_date = today        # one entry per day
    return state, status


def tick(state: BotState, deps: Deps, now) -> BotState:
    """One bot cycle: reconcile (may halt) -> manage open positions -> enter if eligible."""
    today = now.strftime("%Y-%m-%d")
    state, reconcile_ok = run_reconcile_cycle(state, deps)
    state, _ = run_management_cycle(state, deps, today)   # ALWAYS runs (stops must fire)
    if reconcile_ok:
        state, _ = run_entry_cycle(state, deps, now)
    return state
