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
    """Bot state. NOTE: `halted` persists across ticks (and restarts, via state_store). A
    "failed close" halt is sticky until an operator calls clear_halt(); a transient "reconcile
    drift" halt self-clears once a later reconcile comes back clean (see run_reconcile_cycle)."""
    open_positions: list = field(default_factory=list)   # list[ManagedPosition]
    halted: bool = False
    halt_reason: str = ""
    last_entry_date: str = ""                             # the day the entries_today counter applies to
    entries_today: int = 0                                # entries opened so far on last_entry_date
    missing_streak: dict = field(default_factory=dict)    # position_key -> consecutive missing-at-broker reconciles

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
    max_entries_per_day: int = 1             # max NEW positions opened per calendar day
    shared_account: bool = False             # True when multiple bots share ONE broker account
    trade_log: callable = (lambda record: None)   # (dict) -> None; per-bot trade/P&L log sink
    regime_provider: callable = None                 # () -> RegimeState | None ; Phase 0: logged only
    regime_log: callable = (lambda state, ts, event: None)   # (RegimeState, ts, event) -> None


MISSING_REMOVE_THRESHOLD = 2   # consecutive missing-at-broker reconciles before we stop tracking
                               # a position (debounce vs a transient empty positions read)


def run_reconcile_cycle(state: BotState, deps: Deps) -> tuple:
    try:
        drift = reconcile(state.open_positions, deps.broker_positions(),
                          deps.bot_equity(), deps.broker_equity(),
                          ignore_untracked=deps.shared_account, qty_at_least=deps.shared_account)
    except Exception as exc:  # reconcile failure must NOT prevent management/stops from running
        deps.alert_sink([Alert(Severity.CRITICAL, f"reconcile failed: {exc}")])
        return state, False

    # A position the broker no longer has is closed (a filled close we failed to record, assignment,
    # or expiry). Debounce-remove it instead of halting and retrying a close forever for a position
    # that's gone (the 2026-06-29 retry storm). The debounce survives a one-tick transient empty read.
    missing_keys = {position_key(p) for p in drift.missing_at_broker}
    removed = []
    for p in state.open_positions:
        k = position_key(p)
        if k in missing_keys:
            state.missing_streak[k] = state.missing_streak.get(k, 0) + 1
            if state.missing_streak[k] >= MISSING_REMOVE_THRESHOLD:
                removed.append(p)
        else:
            state.missing_streak.pop(k, None)     # present at broker -> reset its debounce
    if removed:
        rem_keys = {position_key(p) for p in removed}
        state.open_positions = [p for p in state.open_positions if position_key(p) not in rem_keys]
        for p in removed:
            state.missing_streak.pop(position_key(p), None)
        deps.alert_sink([Alert(Severity.WARN,
            f"reconciled-away (closed at broker) {p.ticker} {p.short_strike}/{p.long_strike}")
            for p in removed])
        if state.halted and state.halt_reason in ("failed close", "reconcile drift"):
            state.clear_halt()                    # the phantom that caused the halt is gone

    # Halt only on drift the debounce does NOT resolve: an untracked broker position (strict mode),
    # a qty mismatch, or an equity gap. Missing positions are handled above, not halted on.
    other_drift = (bool(drift.untracked_at_broker) or bool(drift.qty_mismatch)
                   or abs(drift.equity_drift) > 50.0)
    if other_drift:
        deps.alert_sink([Alert(Severity.CRITICAL,
            f"reconcile drift: equity={drift.equity_drift} "
            f"missing={len(drift.missing_at_broker)} untracked={len(drift.untracked_at_broker)} "
            f"qty_mismatch={len(drift.qty_mismatch)}")])
        state.halted = True
        state.halt_reason = "reconcile drift"
    elif state.halted and state.halt_reason == "reconcile drift":
        state.clear_halt()        # transient drift resolved (broker re-synced) -> resume entries
    return state, True


def run_management_cycle(state: BotState, deps: Deps, today: str) -> tuple:
    results = monitor_positions(
        state.open_positions,
        mark_fn=deps.mark_position,
        dte_fn=lambda p: deps.dte_of(p, today),
        close_fn=deps.close_spread,
        cfg=deps.manage_cfg,
    )
    for r in results:                                    # per-bot trade log (for A/B measurement)
        rec = {"event": "CLOSE", "date": today, "ticker": r.position.ticker,
               "short": r.position.short_strike, "long": r.position.long_strike,
               "expiry": r.position.expiry, "qty": r.position.qty, "credit": r.position.credit,
               "action": r.action.value, "exit_value": r.value, "status": r.close_status}
        if not r.failed and r.value is not None:
            # realized P&L estimate = (credit collected - debit to close) * 100 * contracts
            rec["pnl"] = round((r.position.credit - r.value) * 100 * r.position.qty, 2)
        deps.trade_log(rec)
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
    if state.last_entry_date != today:
        state.entries_today = 0          # new calendar day -> reset the daily entry counter
    # `now` MUST be in US/Eastern (the live wiring is responsible for that). Enter only 10:00-15:59 ET.
    # Gates: not halted; allowed entry weekday (A/B variable); within RTH; under the concurrent-
    # position cap; and under the per-day entry cap.
    if (state.halted or now.weekday() not in deps.entry_days or not (10 <= now.hour < 16)
            or len(state.open_positions) >= deps.max_open
            or state.entries_today >= deps.max_entries_per_day):
        return state, None
    spot = deps.get_spot("SPY")
    atr = deps.get_atr("SPY")
    expiry = deps.pick_expiry(today)
    order = build_spread_order(spot, atr, deps.get_chain("SPY", expiry), deps.s2b_cfg)
    if order is None:
        return state, "no_order"
    # Never STACK an identical spread (same strikes+expiry): the broker aggregates same-symbol legs,
    # but the position model keys on (ticker,short,long,expiry), so a duplicate collapses to one key
    # and breaks reconcile (qty_mismatch -> halt). Skip until a different strike or the position closes.
    if any(p.short_strike == order.short_strike and p.long_strike == order.long_strike
           and p.expiry == expiry for p in state.open_positions):
        return state, "duplicate_strikes"
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
        state.last_entry_date = today
        state.entries_today += 1             # count toward the per-day entry cap
        deps.trade_log({"event": "OPEN", "date": today, "ticker": "SPY",
                        "short": order.short_strike, "long": order.long_strike, "expiry": expiry,
                        "qty": order.qty, "credit": order.credit, "status": status})
    return state, status


def tick(state: BotState, deps: Deps, now) -> BotState:
    """One bot cycle: reconcile (may halt) -> manage open positions -> enter if eligible.
    PHASE 0: also compute + shadow-log the market regime. This MUST NOT change any decision above,
    so it runs last, inside a try/except that swallows everything (a regime fault never halts trading)."""
    today = now.strftime("%Y-%m-%d")
    state, reconcile_ok = run_reconcile_cycle(state, deps)
    state, _ = run_management_cycle(state, deps, today)   # ALWAYS runs (stops must fire)
    if reconcile_ok:
        state, _ = run_entry_cycle(state, deps, now)
    if deps.regime_provider is not None:                  # Phase 0 instrument: observe only
        try:
            rs = deps.regime_provider()
            if rs is not None:
                deps.regime_log(rs, now.isoformat(), "TICK")
        except Exception as exc:
            deps.alert_sink([Alert(Severity.INFO, f"regime log skipped: {exc}")])
    return state
