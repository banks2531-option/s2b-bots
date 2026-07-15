"""Bot orchestrator: tick = reconcile -> manage -> enter, with halt-gating (spec §3,4,5,7)."""
from dataclasses import dataclass, field, replace

from bot.sizing import contracts_for_risk, regime_adjusted_risk_pct
from bot.risk_gate import RiskGate, RiskConfig
from bot.strategy.s2b import build_spread_order, S2bConfig, to_tradier_payload, _occ
from bot.strategy.execution_price import (quotes_valid, package_too_wide,
                                           expected_executable_credit, spot_moved_too_far)
from bot.features import S2bFeatures
from bot.broker.order_state import result_status
from bot.strategy.manage import (monitor_positions, ManageConfig, ManagedPosition, ExitAction,
                                  dte_from_expiry)
from bot.strategy.credit_quality import dte_bucket, full_size_threshold, credit_tier, _percentile
from bot.strategy.cost_gate import cost_gate_eval
from bot.portfolio.risk_budget import (size_qty, cap_to_budgets, remaining_stop_risk,
                                        planned_stop_loss_per_contract, structural_max_loss_per_contract)
from bot.portfolio.gap_stress import gap_stress_losses
from bot.portfolio import exposure
from bot.ops.ledger import reconcile, position_key
from bot.ops.monitor import alerts_for_cycle, should_halt_new_entries, Alert, Severity
from bot.regime.shadow_monitor import compute_shadow_signals, shadow_caution_score
from bot.research.markouts import MarkoutTracker


# §13 shadow-monitor raw input keys (compute_shadow_signals' required kwargs). Used as the safe
# no-op default for Deps.shadow_data so an unwired bot (or a test) that never overrides it still
# feeds compute_shadow_signals a complete (all-None) kwarg set rather than raising a TypeError.
_SHADOW_INPUT_KEYS = ("spy", "spy_vwap", "spy_atr", "session_high", "session_low", "opening_range",
                     "qqq_ret", "dia_ret", "soxx_ret", "spy_ret", "qqq_vs_vwap", "soxx_vs_vwap",
                     "vix", "vix1d", "put_skew", "short_delta", "short_gamma", "short_iv",
                     "breadth", "up_down_vol", "whale_flow")


def _default_shadow_data():
    return {k: None for k in _SHADOW_INPUT_KEYS}


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
    prev_flow_bias: str = ""                              # UW flow_bias from the PRIOR tick, to detect a bull->bear flip
    credit_ratio_history: dict = field(default_factory=dict)  # DTE-bucket -> list of prior credit/wing_width ratios
    realized_today: float = 0.0                           # sum of realized P&L from closes on `risk_day` (partner review v2 §11)
    risk_day: str = ""                                    # the calendar date `realized_today` applies to
    markout_pending: list = field(default_factory=list)   # persisted MarkoutTracker pending state
                                                           # (partner review v2 §14); round-trips
                                                           # via state_store. Only ever populated
                                                           # when deps.features.regime_shadow_monitor
                                                           # is on -- stays [] forever for Bot C.
    markout_seq: int = 0                                  # monotonic counter -> unique markout signal_ids

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
    open_spread: callable          # (payload) -> ExecutionResult (spec §8; status "filled" on success)
    close_spread: callable         # (ManagedPosition, action) -> ExecutionResult
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
    regime_provider: callable = None                 # () -> RegimeState | None
    regime_log: callable = (lambda state, ts, event: None)   # (RegimeState, ts, event) -> None
    trend_gate_enabled: bool = False                 # Phase 1: pause entries when SPY < 200d MA (risk_off)
    degross_on_risk_off: bool = False                # Phase 1.5: close held positions in a risk_off downtrend
    degross_on_flow_flip: bool = False               # Flow-flip de-gross: close same-day, not-yet-profitable positions on a bull->bear flow flip
    flow_degross_same_day_only: bool = True          # restrict flow-flip de-gross to positions opened TODAY (recency)
    features: object = field(default_factory=S2bFeatures)   # partner review v2 feature flags + thresholds (opt-in, OFF by default)
    risk_equity: callable = None            # () -> float; min(allocated_equity, broker_equity) (partner review v2 §2)
    account_spy_exposure: callable = None   # () -> {"structural": float, "stop": float} across EVERY SPY spread at the broker
    account_spy_spreads: callable = None    # () -> list[ManagedPosition]; the RAW broker-wide SPY spread list (own + foreign),
                                             # used to derive foreign-only exposure via exposure.foreign_spy_exposure (§2)
    option_greeks_iv: callable = None       # (list[occ_symbol]) -> {occ_symbol: mid_iv float}; ONE batched Tradier
                                             # greeks fetch for the BS gap-stress iv_fn (Priority-0 fix item 5). None ->
                                             # iv_fn uses VIX-derived/gap_fallback_iv only. ONLY invoked on the gated
                                             # Bot-B gap-stress paths (via _make_gap_iv_resolver's lazy prime), so the
                                             # ungated live Bot C never calls it (zero new API traffic).
    shadow_data: callable = _default_shadow_data   # () -> dict of §13 raw inputs (partner review v2 §13).
                                             # ONLY called when deps.features.regime_shadow_monitor is on;
                                             # best-effort, wired for real in bot.app.wiring.build_deps.
    markout_log: callable = (lambda record: None)   # (dict) -> None; SEPARATE research-log sink for
                                             # §14 entry markouts (partner review v2 §14) -- never the
                                             # trade log. ONLY called when deps.features.regime_shadow_monitor
                                             # is on; best-effort, wired for real in bot.app.wiring.build_deps.


MISSING_REMOVE_THRESHOLD = 2   # consecutive missing-at-broker reconciles before we stop tracking
                               # a position (debounce vs a transient empty positions read)


def _accumulate_realized(state: BotState, today: str, pnl: float) -> None:
    """Continuous daily-risk gate (partner review v2 §11): fold a close's realized P&L into
    state.realized_today, resetting the running total (and state.risk_day) the first time we see
    a close land on a new calendar day. Unconditional (not gated behind aggregate_risk_budget) --
    the running total must be ready the moment the flag is later flipped on."""
    if today != state.risk_day:
        state.realized_today = 0.0
        state.risk_day = today
    state.realized_today += pnl


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
            if deps.features.actual_fill_accounting:
                # §8: realized P&L must use the ACTUAL close fill, never the triggering mark;
                # report BOTH gross and net (net subtracts opening + closing fees/commissions).
                actual_close = getattr(r.close_result, "average_fill_price", None)
                close_value = actual_close if actual_close is not None else r.value
                gross_pnl = round((r.position.credit - close_value) * 100 * r.position.qty, 2)
                close_commissions = getattr(r.close_result, "commissions", 0.0) or 0.0
                close_reg_fees = getattr(r.close_result, "regulatory_fees", 0.0) or 0.0
                net_pnl = round(gross_pnl - r.position.opening_fees
                                - close_commissions - close_reg_fees, 2)
                rec["gross_pnl"] = gross_pnl
                rec["net_pnl"] = net_pnl
                rec["pnl"] = net_pnl                       # the bottom-line number, now cost-aware
            else:
                # flag OFF -> byte-identical to before: (credit - triggering mark) * 100 * qty
                rec["pnl"] = round((r.position.credit - r.value) * 100 * r.position.qty, 2)
            _accumulate_realized(state, today, rec["pnl"])   # partner review v2 §11 (always on)
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


def run_degross_cycle(state: BotState, deps: Deps, today: str, regime) -> tuple:
    """PHASE 1.5 defensive de-gross (validated 76yr trend-following, p=0.001): on a CONFIRMED downtrend
    (regime.trend_regime == 'risk_off'), REDUCE exposure by closing the positions management chose to
    hold — the put-selling analog of trend-following's "go to flat in a downtrend". Phase 1 already
    pauses new entries, so once flat the book stays flat until risk_on returns (no whipsaw).

    Best-effort and fail-safe: opt-in (deps.degross_on_risk_off); only acts on a positive risk_off (a
    regime fault → None → no liquidation); a close that doesn't fill is alerted and retried next tick
    but NEVER halts (de-gross is discretionary risk reduction, not a stop — no retry-storm, no sticky
    halt); one position's error does not block de-grossing the others."""
    if not (deps.degross_on_risk_off and regime is not None
            and getattr(regime, "trend_regime", "unknown") == "risk_off"):
        return state, []
    closed = []
    for p in list(state.open_positions):
        try:
            value = deps.mark_position(p)
            close_result = deps.close_spread(p, ExitAction.DEGROSS)
            status = result_status(close_result)
        except Exception as exc:   # one position's error must NOT block the others' de-gross
            deps.alert_sink([Alert(Severity.WARN,
                f"de-gross close error {p.ticker} {p.short_strike}/{p.long_strike}: {exc}")])
            continue
        if str(status).lower() == "filled":
            closed.append(p)
            rec = {"event": "DEGROSS", "date": today, "ticker": p.ticker,
                   "short": p.short_strike, "long": p.long_strike, "expiry": p.expiry,
                   "qty": p.qty, "credit": p.credit, "action": ExitAction.DEGROSS.value,
                   "exit_value": value, "status": status}
            if value is not None:
                if deps.features.actual_fill_accounting:
                    # §8: same accounting as run_management_cycle -- use the ACTUAL close fill,
                    # never the triggering mark, and log both gross and net (fee-aware) P&L.
                    actual_close = getattr(close_result, "average_fill_price", None)
                    close_value = actual_close if actual_close is not None else value
                    gross_pnl = round((p.credit - close_value) * 100 * p.qty, 2)
                    close_commissions = getattr(close_result, "commissions", 0.0) or 0.0
                    close_reg_fees = getattr(close_result, "regulatory_fees", 0.0) or 0.0
                    net_pnl = round(gross_pnl - p.opening_fees
                                    - close_commissions - close_reg_fees, 2)
                    rec["gross_pnl"] = gross_pnl
                    rec["net_pnl"] = net_pnl
                    rec["pnl"] = net_pnl
                else:
                    # flag OFF -> byte-identical to before: (credit - triggering mark) * 100 * qty
                    rec["pnl"] = round((p.credit - value) * 100 * p.qty, 2)
                _accumulate_realized(state, today, rec["pnl"])   # partner review v2 §11 (always on)
            deps.trade_log(rec)
        else:                      # didn't fill: alert + retry next tick, but do NOT halt
            deps.alert_sink([Alert(Severity.WARN,
                f"de-gross close not filled ({status}) {p.ticker} {p.short_strike}/{p.long_strike}")])
    if closed:
        ck = {position_key(p) for p in closed}
        state.open_positions = [p for p in state.open_positions if position_key(p) not in ck]
        deps.alert_sink([Alert(Severity.WARN,
            f"de-grossed {len(closed)} position(s) on a confirmed risk_off downtrend")])
    return state, closed


def run_flow_degross_cycle(state: BotState, deps: Deps, today: str, regime) -> tuple:
    """FLOW-FLIP de-gross (backtested): when market-wide options flow flips from BULLISH to BEARISH
    across ticks (state.prev_flow_bias == 'bullish' and regime.flow_bias == 'bearish'), CLOSE any
    open position that is BOTH (a) opened TODAY (same-day / very recent) AND (b) not yet profitable
    (debit-to-close mark >= entry credit). Profitable positions, and positions opened on a prior day,
    are LEFT to normal management.

    Best-effort and fail-safe (mirrors run_degross_cycle exactly): opt-in (deps.degross_on_flow_flip);
    only acts on a positive bull->bear flip with a live regime (a regime fault -> None -> no action);
    a close that doesn't fill is alerted and retried next tick but NEVER halts; one position's error
    does not block de-grossing the others."""
    if not (deps.degross_on_flow_flip and regime is not None
            and state.prev_flow_bias == "bullish"
            and getattr(regime, "flow_bias", "neutral") == "bearish"):
        return state, []
    closed = []
    for p in list(state.open_positions):
        # recency gate: only same-day positions (unless the same-day restriction is disabled)
        if deps.flow_degross_same_day_only and getattr(p, "entry_date", "") != today:
            continue
        try:
            value = deps.mark_position(p)
            if value < p.credit:          # already profitable -> leave to normal management
                continue
            close_result = deps.close_spread(p, ExitAction.FLOW_DEGROSS)
            status = result_status(close_result)
        except Exception as exc:   # one position's error must NOT block the others' de-gross
            deps.alert_sink([Alert(Severity.WARN,
                f"flow-degross close error {p.ticker} {p.short_strike}/{p.long_strike}: {exc}")])
            continue
        if str(status).lower() == "filled":
            closed.append(p)
            rec = {"event": "FLOW_DEGROSS", "date": today, "ticker": p.ticker,
                   "short": p.short_strike, "long": p.long_strike, "expiry": p.expiry,
                   "qty": p.qty, "credit": p.credit, "action": ExitAction.FLOW_DEGROSS.value,
                   "exit_value": value, "status": status}
            if value is not None:
                if deps.features.actual_fill_accounting:
                    # §8: same accounting as run_management_cycle -- use the ACTUAL close fill,
                    # never the triggering mark, and log both gross and net (fee-aware) P&L.
                    actual_close = getattr(close_result, "average_fill_price", None)
                    close_value = actual_close if actual_close is not None else value
                    gross_pnl = round((p.credit - close_value) * 100 * p.qty, 2)
                    close_commissions = getattr(close_result, "commissions", 0.0) or 0.0
                    close_reg_fees = getattr(close_result, "regulatory_fees", 0.0) or 0.0
                    net_pnl = round(gross_pnl - p.opening_fees
                                    - close_commissions - close_reg_fees, 2)
                    rec["gross_pnl"] = gross_pnl
                    rec["net_pnl"] = net_pnl
                    rec["pnl"] = net_pnl
                else:
                    # flag OFF -> byte-identical to before: (credit - triggering mark) * 100 * qty
                    rec["pnl"] = round((p.credit - value) * 100 * p.qty, 2)
                _accumulate_realized(state, today, rec["pnl"])   # partner review v2 §11 (always on)
            deps.trade_log(rec)
        else:                      # didn't fill: alert + retry next tick, but do NOT halt
            deps.alert_sink([Alert(Severity.WARN,
                f"flow-degross close not filled ({status}) {p.ticker} {p.short_strike}/{p.long_strike}")])
    if closed:
        ck = {position_key(p) for p in closed}
        state.open_positions = [p for p in state.open_positions if position_key(p) not in ck]
        deps.alert_sink([Alert(Severity.WARN,
            f"flow-degrossed {len(closed)} same-day position(s) on a bull->bear flow flip")])
    return state, closed


def run_shadow_monitor_cycle(state: BotState, deps: Deps, today: str) -> None:
    """Partner review v2 §13: LOG-ONLY downturn-detection shadow monitor. Computes the §13 signal
    set, a bounded caution score, and the shadow action the (not-yet-live) defensive engine WOULD
    have taken -- and writes it all to the trade log as a SHADOW record. Reads state; NEVER writes
    it, never touches an order, and never changes any live decision.

    No-op unless deps.features.regime_shadow_monitor is on (Bot C / run_s2b_live.py: always off).
    Best-effort: any failure fetching/computing the signals (a down feed, a bad shadow_data())
    is swallowed -- a SHADOW logging failure must never affect trading, and must never raise out
    of tick()."""
    if not deps.features.regime_shadow_monitor:
        return
    try:
        raw = deps.shadow_data()
        signals = compute_shadow_signals(**raw)
        score, action = shadow_caution_score(signals)
        rec = {"event": "SHADOW", "date": today, "shadow_score": score, "shadow_action": action}
        rec.update(signals)
        deps.trade_log(rec)
    except Exception:
        pass


def run_markout_cycle(state: BotState, deps: Deps, now) -> None:
    """§14 (partner review v2): once per tick, advance every pending research markout's due
    horizons (1/5/15/30/60 min) off the current SPY spot, and write the EOD row once the session
    is past RTH close (>= 16:00 ET) -- see bot/research/markouts.py for the resolution logic.

    The spread-value marker reuses deps.mark_position against a synthetic ManagedPosition built
    from the pending item's own strikes/expiry (mark_position only reads leg quotes off
    ticker/expiry/short_strike/long_strike, so this works for a candidate spread that was NEVER
    actually opened at the broker -- exactly what §14 requires for rejected signals).

    LOG ONLY: reads/writes only state.markout_pending via MarkoutTracker; never touches an order,
    a size, or any trading decision. No-op unless deps.features.regime_shadow_monitor is on (Bot C:
    always off). Best-effort: any failure (a down feed, a bad quote) is swallowed -- a markout
    logging failure must never affect trading, and must never raise out of tick()."""
    if not deps.features.regime_shadow_monitor:
        return
    try:
        tracker = MarkoutTracker.from_state(state.markout_pending, deps.markout_log)

        def _spread_value(item):
            pos = ManagedPosition(item["ticker"], item["short_strike"], item["long_strike"],
                                  credit=0.0, qty=1, expiry=item["expiry"])
            return deps.mark_position(pos)

        spy_now = deps.get_spot("SPY")
        tracker.resolve_due(now, spy_now, _spread_value)
        if now.hour >= 16:                    # past RTH close -> finalize the day's EOD result
            tracker.finalize_eod(spy_now, _spread_value)
        state.markout_pending = tracker.to_state()
    except Exception:
        pass


def _make_gap_iv_resolver(deps, regime):
    """Build the (prime, iv_fn) pair used by the BS gap-stress reprice (Priority-0 fix item 5).

    Per-leg IV is sourced in priority order:
      1. REAL market mid_iv per leg -- from a SINGLE batched, memoized Tradier greeks fetch
         (deps.option_greeks_iv), primed once per entry cycle from the whole gap-stress book;
      2. VIX-derived (regime.vix_level / 100, a 30d annualized vol);
      3. features.gap_fallback_iv.

    iv_fn ALWAYS returns a positive (short_iv, long_iv) -- never None/raises/<=0 -- so foreign
    spreads and any leg whose greeks are missing still get a finite stress. The stressed put-skew
    bump is applied ON TOP of these real IVs inside the BS grid (features.gap_skew_bump), separately.

    GUARDRAIL: the greeks fetch is LAZY -- it only runs when `prime` is actually called, which only
    happens on the gated gap-stress paths (aggregate_risk_budget enforcement / decision_logging
    telemetry). Both are OFF on the live Bot C, so Bot C triggers ZERO greeks fetches. Factored out
    of run_entry_cycle so iv_fn/prime are unit-testable in isolation."""
    base_iv = deps.features.gap_fallback_iv
    vl = getattr(regime, "vix_level", None) if regime is not None else None
    if isinstance(vl, (int, float)) and vl > 0:
        base_iv = float(vl) / 100.0
    if not (base_iv and base_iv > 0):
        base_iv = deps.features.gap_fallback_iv

    cache = {}          # OCC option symbol -> real market mid_iv (>0)
    fetched = [False]   # one-shot latch: the batched greeks fetch happens AT MOST once per cycle

    def leg_symbol(p, strike):
        return _occ(getattr(p, "ticker", "SPY") or "SPY", getattr(p, "expiry", None), "P", strike)

    def prime(book):
        """Batched, memoized real-greeks fetch for EVERY leg symbol in `book` (own + foreign +
        proposed). One deps.option_greeks_iv call for all symbols; runs at most once per cycle;
        skipped entirely when deps.option_greeks_iv is unwired. Best-effort -- never raises."""
        if fetched[0]:
            return
        fetched[0] = True   # latch BEFORE the call so even a raising fetch can't retry-storm
        if deps.option_greeks_iv is None:
            return
        syms = set()
        for p in book:
            if not getattr(p, "expiry", None):
                continue
            try:
                syms.add(leg_symbol(p, p.short_strike))
                syms.add(leg_symbol(p, p.long_strike))
            except Exception:
                pass
        if not syms:
            return
        try:
            got = deps.option_greeks_iv(sorted(syms)) or {}
            for k, v in got.items():
                if v is not None and v > 0:
                    cache[k] = float(v)
        except Exception:
            pass

    def iv_fn(position):
        """(short_iv, long_iv): real per-leg mid_iv when cached (>0), else VIX-derived, else fallback."""
        out = []
        for strike in (position.short_strike, position.long_strike):
            v = None
            try:
                v = cache.get(leg_symbol(position, strike))
            except Exception:
                v = None
            out.append(v if (v is not None and v > 0) else base_iv)
        return (out[0], out[1])

    return prime, iv_fn


def _decision_telemetry(state: BotState, deps: Deps, today, spot=None, atr=None, expiry=None, order=None,
                        foreign_positions=(), iv_fn=None, prime_iv=None):
    """Best-effort per-entry exposure snapshot for the DECISION log (partner review v2 §12): positions
    opened today, positions in the target expiration, distance between adjacent short strikes
    (including the proposed spread, when one exists), aggregate remaining stop risk, aggregate
    structural risk, and aggregate 1.5-ATR gap-stress loss. NEVER raises: anything not computable at
    a given return point (e.g. rejected before spot/atr/expiry/order were known) is left None.

    NOTE: aggregate SPY delta/gamma are deferred to the Phase-4 shadow monitor (spec §13) -- omitted
    here by design, not an oversight."""
    tel = {"positions_today": None, "positions_in_expiry": None, "adjacent_strike_distance": None,
           "agg_remaining_stop": None, "agg_structural": None, "agg_gap_stress_1_5": None}
    try:
        tel["positions_today"] = sum(1 for p in state.open_positions if p.entry_date == today)
    except Exception:
        pass
    try:
        wing_width = deps.s2b_cfg.wing_width
        tel["agg_remaining_stop"] = round(sum(
            remaining_stop_risk(p.credit, deps.mark_position(p), p.qty, deps.features.expected_stop_slippage)
            for p in state.open_positions), 2)
        tel["agg_structural"] = round(sum(
            structural_max_loss_per_contract(wing_width, p.credit) * p.qty
            for p in state.open_positions), 2)
    except Exception:
        pass
    if expiry is not None:
        try:
            same_expiry = [p for p in state.open_positions if p.expiry == expiry]
            tel["positions_in_expiry"] = len(same_expiry)
            strikes = {p.short_strike for p in same_expiry}
            if order is not None:
                strikes.add(order.short_strike)
            strikes = sorted(strikes)
            if len(strikes) >= 2:
                tel["adjacent_strike_distance"] = min(b - a for a, b in zip(strikes, strikes[1:]))
        except Exception:
            pass
    if spot is not None and atr is not None:
        try:
            # Priority-0 fix item 6: foreign SPY spreads are folded into the gap-stress book here so
            # the DECISION row's agg_gap_stress_1_5 is book-aligned with the enforcement calc (and the
            # OPEN row) whenever a foreign position exists. NOTE: this stresses the proposed leg at
            # order.credit whereas enforcement stresses it at risk_credit (exec_credit); those differ
            # only when exec_credit != order.credit (credit-tier/cost path), a pre-existing
            # telemetry/credit nuance to be revisited in the gap-stress rework (Task 5).
            positions_for_stress = (list(state.open_positions) + list(foreign_positions)
                                    + ([order] if order is not None else []))
            # Priority-0 fix item 5: same BS model + IV source + `today` as the §10 enforcement calc,
            # so this logged agg_gap_stress_1_5 stays == the enforced/OPEN-row gap_stress_1_5 (holds
            # exactly when risk_credit == order.credit; see the enforcement-site note). prime_iv is the
            # SAME memoized resolver, so the batched greeks fetch is shared (no extra call here).
            if prime_iv is not None:
                prime_iv(positions_for_stress)
            tel["agg_gap_stress_1_5"] = gap_stress_losses(
                positions_for_stress, spot, atr, deps.s2b_cfg.wing_width,
                today=today, iv_fn=iv_fn, f=deps.features)[1.5]
        except Exception:
            pass
    return tel


def run_entry_cycle(state: BotState, deps: Deps, now, regime=None) -> tuple:
    today = now.strftime("%Y-%m-%d")
    if state.last_entry_date != today:
        state.entries_today = 0          # new calendar day -> reset the daily entry counter

    # Priority-0 fix item 5: per-leg IV source for the BS gap-stress reprice, built once and shared by
    # ALL THREE gap-stress call sites (telemetry + enforcement + shrink-loop) so logged == enforced.
    # The resolver primes a batched, memoized real-greeks fetch (mid_iv) lazily -- see
    # _make_gap_iv_resolver -- so an ungated live-path bot never triggers a fetch.
    _prime_leg_ivs, iv_fn = _make_gap_iv_resolver(deps, regime)

    exec_credit = None       # §4 conservative expected-executable credit; set once quote guards pass
    credit_telemetry = None  # §3: credit_ratio/credit_pctl40/credit_sample_count/credit_threshold/
                              # credit_quality_mult; set once the credit-tiers block runs (credit_tiers on)
    cost_telemetry = None    # §5: cost_gross_target/cost_round_trip/cost_target_ratio; set once the
                              # transaction-cost gate block runs (transaction_cost_gate on)
    risk_budget_telemetry = None   # Priority-0 fix item 7: which of the four aggregate risk budgets
                                    # (same_day_stop/expiry_stop/total_stop/total_structural) bound
                                    # a risk_budget reject, + its exposure/limit/headroom numbers;
                                    # set once the aggregate_risk_budget block runs and caps to 0
    entry_delta = None       # §14: best-effort short-put delta at signal time, set once the chain is
                              # fetched and an order is built; feeds _record_markout below
    foreign_position_list = []   # Priority-0 fix item 6: quantity-aware foreign SPY spreads at the
                                  # broker; set once the aggregate_risk_budget block runs. Initialized
                                  # here (before any _log_decision -> _decision_telemetry call on an
                                  # early-reject path) so the DECISION row's gap-stress book stays
                                  # aligned with the OPEN row's, and so early rejects don't UnboundLocalError.

    def _record_markout(reason, order, expiry, spot):
        """§14 (partner review v2): record ONE research markout signal for this evaluated
        candidate (filled OR rejected -- reason=="filled" is the only fill marker). Independently
        gated on regime_shadow_monitor (NOT decision_logging) so markouts are captured even when
        decision logging itself is off. LOG ONLY: only ever appends to state.markout_pending;
        NEVER touches an order/size/decision. Best-effort -- never raises out of run_entry_cycle."""
        try:
            state.markout_seq += 1
            tracker = MarkoutTracker.from_state(state.markout_pending, deps.markout_log)
            cfg = deps.manage_cfg
            filled = (reason == "filled")
            tracker.record_signal(now, {
                "signal_id": f"{today}-{state.markout_seq}", "ticker": order.ticker,
                "short_strike": order.short_strike, "long_strike": order.long_strike,
                "expiry": expiry, "filled": filled,
                "entry_spy": spot, "entry_spread_value": order.credit,
                "credit": order.credit, "qty": order.qty,
                "entry_delta": entry_delta, "entry_iv": None,
                "tp_value": (round(order.credit * (1 - cfg.tp_pct), 4) if filled else None),
                "stop_value": (round(order.credit * (1 + cfg.stop_mult), 4) if filled else None),
            })
            state.markout_pending = tracker.to_state()
        except Exception:
            pass

    def _log_decision(reason, spot=None, atr=None, expiry=None, order=None):
        """§1/§12: write a DECISION record (reason + active flags + best-effort exposure telemetry)
        to the trade log. No-op unless deps.features.decision_logging is on; never raises.

        Also independently triggers the §14 research markout record (_record_markout) whenever
        regime_shadow_monitor is on and a concrete candidate (order) exists -- that piece runs
        regardless of decision_logging, since the two flags are orthogonal."""
        if deps.features.regime_shadow_monitor and order is not None:
            _record_markout(reason, order, expiry, spot)
        if not deps.features.decision_logging:
            return
        try:
            flags = {"credit_tiers": deps.features.credit_tiers,
                      "transaction_cost_gate": deps.features.transaction_cost_gate,
                      "entry_price_ladder": deps.features.entry_price_ladder,
                      "tp_price_ladder": deps.features.tp_price_ladder,
                      "aggregate_risk_budget": deps.features.aggregate_risk_budget,
                      "actual_fill_accounting": deps.features.actual_fill_accounting,
                      "regime_shadow_monitor": deps.features.regime_shadow_monitor}
        except Exception:
            flags = {}
        rec = {"event": "DECISION", "date": today, "decision": reason, "flags": flags}
        if exec_credit is not None:   # §4: the conservative expected-executable credit, once known
            rec["expected_executable_credit"] = round(exec_credit, 4)
        if credit_telemetry is not None:   # §3: credit-tier decision inputs, once computed
            rec.update(credit_telemetry)
        if cost_telemetry is not None:   # §5: cost-gate decision inputs, once computed
            rec.update(cost_telemetry)
        if risk_budget_telemetry is not None:   # item 7: which budget bound a risk_budget reject
            rec.update(risk_budget_telemetry)
        rec.update(_decision_telemetry(state, deps, today, spot=spot, atr=atr, expiry=expiry, order=order,
                                        foreign_positions=foreign_position_list, iv_fn=iv_fn,
                                        prime_iv=_prime_leg_ivs))
        try:
            deps.trade_log(rec)
        except Exception:
            pass

    # `now` MUST be in US/Eastern (the live wiring is responsible for that). Enter only 10:00-15:59 ET.
    # Gates: not halted; allowed entry weekday (A/B variable); within RTH; under the concurrent-
    # position cap; and under the per-day entry cap. Each gate is checked individually (rather than
    # one combined condition) so the DECISION log can record WHICH gate blocked the entry; the
    # returned `info` stays None for all five, exactly as before (byte-identical behavior).
    if state.halted:
        _log_decision("halted")
        return state, None
    if now.weekday() not in deps.entry_days:
        _log_decision("wrong_weekday")
        return state, None
    if not (10 <= now.hour < 16):
        _log_decision("off_hours")
        return state, None
    if len(state.open_positions) >= deps.max_open:
        _log_decision("max_open")
        return state, None
    if state.entries_today >= deps.max_entries_per_day:
        _log_decision("max_entries")
        return state, None
    # PHASE 1 defensive guard (validated 76yr, p=0.001): pause new put-selling in a confirmed
    # downtrend (SPY below its 200d MA). Only acts on a positive risk_off signal; an unknown/missing
    # regime falls through to normal trading (a regime fault must not block the validated strategy).
    if deps.trend_gate_enabled and regime is not None and getattr(regime, "trend_regime", "unknown") == "risk_off":
        _log_decision("trend_paused")
        return state, "trend_paused"
    spot = deps.get_spot("SPY")
    atr = deps.get_atr("SPY")
    expiry = deps.pick_expiry(today)
    chain = deps.get_chain("SPY", expiry)
    order = build_spread_order(spot, atr, chain, deps.s2b_cfg)
    if order is None:
        _log_decision("no_order", spot=spot, atr=atr, expiry=expiry)
        return state, "no_order"
    order.expiry = expiry   # item 5: carry expiry on the proposed order so the BS gap-stress reprice
                            # (below + in _decision_telemetry) can derive its DTE, identically on both.
    # §14: best-effort short-put delta at signal time, for the markout record (_record_markout
    # above). None if not found in the chain (IV isn't carried by OptionQuote/parse_chain today).
    for _q in chain:
        if abs(_q.strike - order.short_strike) < 1e-6:
            entry_delta = _q.delta
            break
    # Expected-executable-credit + quote-quality guards (partner review v2 §4), OPT-IN when
    # credit_tiers OR transaction_cost_gate is on (both need the conservative credit downstream).
    # Bot C (both flags off) never enters this block -> byte-identical behavior.
    if deps.features.credit_tiers or deps.features.transaction_cost_gate:
        if order.short_bid is not None:   # leg quotes attached (always true via build_spread_order;
                                           # a hand-built SpreadOrder without quotes skips the guard)
            if not quotes_valid(order.short_bid, order.short_ask, order.long_bid, order.long_ask):
                _log_decision("quote_invalid", spot=spot, atr=atr, expiry=expiry, order=order)
                return state, "quote_invalid"
            if package_too_wide(order.short_bid, order.short_ask, order.long_bid, order.long_ask,
                                 deps.features.max_package_width_ratio):
                _log_decision("quote_wide", spot=spot, atr=atr, expiry=expiry, order=order)
                return state, "quote_wide"
            exec_credit = expected_executable_credit(
                order.short_bid, order.short_ask, order.long_bid, order.long_ask,
                deps.features.expected_entry_slippage)
            # Inert in single-shot entry: signal_spot == order_spot here (no time gap between
            # signal and order within one synchronous cycle). Becomes active once entries are
            # staged across a ladder (Task 3.1), where order_spot is re-read after signal_spot.
            spot_moved_too_far(spot, spot, atr, deps.features.max_signal_to_order_spot_move_atr)
            # TODO(partner review v2 §4): MAX_QUOTE_AGE_SECONDS staleness enforcement is deferred --
            # OptionQuote/parse_chain doesn't carry a quote timestamp yet. Wire this once quote
            # timestamps are plumbed through parse_chain (a later task).
    # Adaptive credit-quality tiering (partner review v2 §3), OPT-IN via deps.features.credit_tiers
    # (False = feature OFF -> this entire block is a no-op, so default behavior is byte-identical to
    # before). Sizes off exec_credit (the conservative expected-executable credit computed by the §4
    # guards above, when available -- falls back to order.credit if quote guards were skipped)
    # rather than the naive mid-quote credit. A credit too thin relative to the wing width is
    # rejected outright; a mid-tier credit is sized down to a "probe"; a strong credit trades full
    # size (see bot/strategy/credit_quality.py).
    quality_multiplier = 1.0
    if deps.features.credit_tiers:
        wing = deps.s2b_cfg.wing_width
        credit_for_tier = exec_credit if exec_credit is not None else order.credit
        dte = dte_from_expiry(expiry, today)
        bucket = dte_bucket(dte)
        ratio = credit_for_tier / wing
        # COPY captured BEFORE appending the current candidate -- no look-ahead: the threshold for
        # THIS decision must only ever see signals from PRIOR decisions.
        prior = list(state.credit_ratio_history.get(bucket, []))
        count = len(prior)
        thr = full_size_threshold(prior, deps.features.full_size_credit_floor,
                                   deps.features.full_size_credit_ceiling,
                                   deps.features.credit_adapt_min_signals)
        # §3: "Use prior candidate signals, including rejected signals" -- append EVERY evaluated
        # candidate's ratio (even one about to be rejected below) so future thresholds see the full
        # population of opportunities, not just fills.
        hist = state.credit_ratio_history.setdefault(bucket, [])
        hist.append(round(ratio, 4))
        state.credit_ratio_history[bucket] = hist[-60:]   # keep only the last 60 entries
        mult = credit_tier(credit_for_tier, wing, deps.features.min_credit_ratio, thr,
                            deps.features.probe_size_multiplier)
        credit_telemetry = {
            "credit_ratio": ratio,
            "credit_pctl40": (_percentile(prior, 40) if count else None),
            "credit_sample_count": count,
            "credit_threshold": thr,
            "credit_quality_mult": mult,
        }
        quality_multiplier = mult
        if mult == 0.0:
            _log_decision("credit_too_low", spot=spot, atr=atr, expiry=expiry, order=order)
            return state, "credit_too_low"
    # Transaction-cost profitability gate (partner review v2 §5), OPT-IN via
    # deps.features.transaction_cost_gate. Runs AFTER the credit-tier decision above (so a candidate
    # already rejected as credit_too_low never reaches here) and BEFORE sizing. Rejects a candidate
    # whose 50%-take-profit gross target doesn't clear min_target_to_cost_ratio x the estimated
    # round-trip cost (synthetic commissions + entry/exit slippage). Sizes off exec_credit (the
    # conservative expected-executable credit, §4) -- falls back to order.credit if it wasn't
    # computed (quote guards skipped because the order has no attached quotes).
    if deps.features.transaction_cost_gate:
        credit_for_cost = exec_credit if exec_credit is not None else order.credit
        cost_eval = cost_gate_eval(credit_for_cost, deps.features)
        cost_telemetry = {
            "cost_gross_target": cost_eval["gross_target"],
            "cost_round_trip": cost_eval["round_trip_cost"],
            "cost_target_ratio": cost_eval["target_to_cost_ratio"],
        }
        if not cost_eval["passes"]:
            _log_decision("cost_gate", spot=spot, atr=atr, expiry=expiry, order=order)
            return state, "cost_gate"
    # Never STACK an identical spread (same strikes+expiry): the broker aggregates same-symbol legs,
    # but the position model keys on (ticker,short,long,expiry), so a duplicate collapses to one key
    # and breaks reconcile (qty_mismatch -> halt). Skip until a different strike or the position closes.
    if any(p.short_strike == order.short_strike and p.long_strike == order.long_strike
           and p.expiry == expiry for p in state.open_positions):
        _log_decision("duplicate_strikes", spot=spot, atr=atr, expiry=expiry, order=order)
        return state, "duplicate_strikes"
    acct = deps.account_state(today, len(state.open_positions))
    pct_rank, change = deps.get_vix_regime()
    risk = regime_adjusted_risk_pct(deps.base_risk_pct, pct_rank, change)
    gap_losses = None   # only populated (and only logged) when aggregate_risk_budget is on
    if deps.features.aggregate_risk_budget:
        # Aggregate dollar-risk budget sizing (partner review v2 §9), OPT-IN. Replaces the
        # equity/risk_pct sizing above with a budgeted qty derived from the entry-stop-risk and
        # structural constraints, then capped so this trade never blows through the book's
        # same-day/expiry/total stop or total structural risk limits. Sizes off exec_credit (the
        # conservative expected-executable credit, §4/§9) -- falls back to order.credit if it
        # wasn't computed (credit_tiers and transaction_cost_gate both off) -- and folds in the
        # credit-tier quality_multiplier from the block above (1.0 when credit_tiers is off).
        req = deps.risk_equity()
        risk_credit = exec_credit if exec_credit is not None else order.credit
        # Foreign SPY positions at the broker (opened by another bot/human, not this one) MUST count
        # toward this bot's TOTAL stop/structural budgets (partner review v2 §2). Derived from the
        # raw broker-wide spread list minus this bot's own open positions (by strikes+expiry), so
        # this bot's own book is never double-counted. No spread-list feed wired -> zero (unchanged).
        broker_spy_spreads = deps.account_spy_spreads() if deps.account_spy_spreads is not None else []
        # Priority-0 fix item 6: quantity-aware -- a broker spread matching an own position's
        # (short,long,expiry) key is no longer excluded wholesale; only the qty in excess of this
        # bot's own tracked qty at that key is foreign. foreign_position_list is reused below to
        # also fold foreign spreads into the gap-stress book (they were previously counted toward
        # the stop/structural budgets but silently absent from the gap-stress stress test).
        foreign_position_list = exposure.foreign_spreads(broker_spy_spreads, state.open_positions)
        foreign = exposure.account_spy_exposure(foreign_position_list)
        proposed_qty = size_qty(req, risk_credit, deps.s2b_cfg.wing_width, deps.features,
                                quality_multiplier=quality_multiplier)
        # Priority-0 fix item 7: cap_to_budgets now returns a RiskBudgetResult naming WHICH of the
        # four budgets (if any) bound the candidate down, not just a bare int -- so a reject can be
        # decision-logged as e.g. "risk_budget:expiry_stop" instead of an undifferentiated
        # "risk_budget". int(budget_result) is used for sizing so behavior/qty is unchanged
        # (RiskBudgetResult is int-compatible via __int__/__eq__ for any other caller too).
        budget_result = cap_to_budgets(proposed_qty, risk_credit, deps.s2b_cfg.wing_width, expiry,
                                       today, state.open_positions, deps.mark_position, req,
                                       deps.features, foreign_exposure=foreign)
        qty = int(budget_result)
        if qty <= 0:
            limiting = budget_result.limiting_budget
            # limiting is None when proposed_qty itself was already <=0 (size_qty rounded to zero
            # before any book budget was even consulted) -- keep the bare "risk_budget" reason in
            # that case since no specific budget can be blamed; only suffix it when a real budget
            # bound (unconstrained-input case is a pure regression: exact same reason as before).
            reason = f"risk_budget:{limiting}" if limiting is not None else "risk_budget"
            # Read exposure/limit straight off the result (keyed on ITS OWN limiting_budget) rather
            # than re-deriving the mapping here -- so a budget rename can't silently drift into None.
            binding_exposure, binding_limit = budget_result.limiting_exposure_and_limit()
            risk_budget_telemetry = {
                "risk_budget_limiting": limiting,
                "risk_budget_exposure": (round(binding_exposure, 2)
                                         if binding_exposure is not None else None),
                "risk_budget_limit": round(binding_limit, 2) if binding_limit is not None else None,
                "risk_budget_headroom": (round(binding_limit - binding_exposure, 2)
                                         if binding_exposure is not None and binding_limit is not None
                                         else None),
                "risk_budget_proposed_qty": proposed_qty,
                "risk_budget_permitted_qty": qty,
            }
            _log_decision(reason, spot=spot, atr=atr, expiry=expiry, order=order)
            return state, "risk_budget"
        order.qty = qty
        # Gap-risk stress test (partner review v2 §10), OPT-IN behind the same flag: stress ALL
        # open positions, FOREIGN positions (Priority-0 fix item 6 -- foreign_position_list, same
        # quantity-aware helper used for the stop/structural budgets above), AND the proposed trade
        # at SPY down 1.0/1.5/2.0 ATR (conservative intrinsic-value repricing). If the 1.5-ATR total
        # stressed loss exceeds the budget, shrink the proposed qty (re-checking each step) until it
        # fits, or reject outright if even 1 contract doesn't. All three scenario losses are
        # recorded for the final qty. The proposed leg is stressed at risk_credit (a `replace()`
        # view -- the real `order` object, used for the broker payload/OPEN record below, is left
        # untouched).
        gap_budget = deps.features.max_gap_stress_loss_pct * req
        # Priority-0 fix item 5: BS shock-grid reprice (worst-cell) instead of intrinsic-only. Uses
        # the SAME memoized iv_fn/today/features as the §12 DECISION-telemetry calc, so the logged
        # agg_gap_stress_1_5 matches this enforced/OPEN-row gap_stress_1_5 -- exactly when
        # risk_credit == order.credit (they diverge only on the credit-tier/cost-gate path, where the
        # proposed leg is stressed at exec_credit here vs order.credit in telemetry; deferred nuance).
        # prime_leg_ivs runs the single batched greeks fetch for the whole book (memoized thereafter).
        gap_stress_book = state.open_positions + foreign_position_list + [replace(order, credit=risk_credit)]
        _prime_leg_ivs(gap_stress_book)
        gap_losses = gap_stress_losses(gap_stress_book, spot, atr, deps.s2b_cfg.wing_width,
                                       today=today, iv_fn=iv_fn, f=deps.features)
        while order.qty > 0 and gap_losses[1.5] > gap_budget:
            order.qty -= 1
            gap_stress_book = state.open_positions + foreign_position_list + [replace(order, credit=risk_credit)]
            gap_losses = gap_stress_losses(gap_stress_book, spot, atr, deps.s2b_cfg.wing_width,
                                           today=today, iv_fn=iv_fn, f=deps.features)
        if order.qty <= 0:
            _log_decision("gap_stress", spot=spot, atr=atr, expiry=expiry, order=order)
            return state, "gap_stress"
        # Continuous daily-risk gate (partner review v2 §11), OPT-IN behind the same flag: don't
        # wait for the first stop to restrict entries. daily_risk_consumption = today's realized
        # loss (if any) + the remaining stop risk of positions already opened TODAY + this
        # proposed trade's own planned stop risk. If that exceeds the same-day stop budget, shrink
        # qty (re-checking each step) until it fits, or reject outright if even 1 doesn't.
        if today != state.risk_day:
            state.realized_today = 0.0
            state.risk_day = today
        today_stop = sum(
            remaining_stop_risk(p.credit, deps.mark_position(p), p.qty, deps.features.expected_stop_slippage)
            for p in state.open_positions if p.entry_date == today)
        proposed_stop = planned_stop_loss_per_contract(risk_credit, deps.s2b_cfg.wing_width,
                                                        deps.features.expected_stop_slippage) * order.qty
        daily_risk_consumption = abs(min(state.realized_today, 0.0)) + today_stop + proposed_stop
        daily_risk_budget = deps.features.max_same_day_stop_risk_pct * req
        while order.qty > 0 and daily_risk_consumption > daily_risk_budget:
            order.qty -= 1
            proposed_stop = planned_stop_loss_per_contract(risk_credit, deps.s2b_cfg.wing_width,
                                                            deps.features.expected_stop_slippage) * order.qty
            daily_risk_consumption = abs(min(state.realized_today, 0.0)) + today_stop + proposed_stop
        if order.qty <= 0:
            _log_decision("daily_risk", spot=spot, atr=atr, expiry=expiry, order=order)
            return state, "daily_risk"
        # ALSO halt new entries (but never management/closing) once today's total P&L (realized +
        # unrealized on every currently-open position) breaches the daily loss-halt threshold.
        unrealized_today = sum((p.credit - deps.mark_position(p)) * 100 * p.qty
                                for p in state.open_positions)
        if state.realized_today + unrealized_today <= -deps.features.daily_pnl_halt_pct * req:
            _log_decision("day_loss_halt", spot=spot, atr=atr, expiry=expiry, order=order)
            return state, "day_loss_halt"
    else:
        order.qty = contracts_for_risk(acct.equity, order.max_loss_per_contract, risk)
        if deps.features.credit_tiers:
            order.qty = max(1, int(order.qty * quality_multiplier))   # a probe of a 1-lot stays 1
    decision = RiskGate(deps.risk_cfg).is_order_allowed(order, acct)
    if not decision.allowed:
        _log_decision(decision.reason, spot=spot, atr=atr, expiry=expiry, order=order)
        return state, decision.reason
    open_result = deps.open_spread(to_tradier_payload(order, expiry, order.qty))
    status = result_status(open_result)
    if status == "filled":
        # §8: with actual_fill_accounting ON, the position is recorded off the ACTUAL fill
        # (price + quantity), never the requested/quoted values; opening fees are captured too.
        # With the flag OFF (default, and always for the live bot) this is byte-identical to
        # before: the requested credit/qty, and opening_fees stays 0.0.
        credit, qty, opening_fees = order.credit, order.qty, 0.0
        if deps.features.actual_fill_accounting:
            fill_price = getattr(open_result, "average_fill_price", None)
            if fill_price is not None:
                credit = fill_price
            filled_qty = getattr(open_result, "filled_quantity", None)
            if filled_qty:
                qty = filled_qty
            commissions = getattr(open_result, "commissions", 0.0) or 0.0
            reg_fees = getattr(open_result, "regulatory_fees", 0.0) or 0.0
            opening_fees = commissions + reg_fees
        # DECISION log BEFORE the new position is folded into state.open_positions, so the exposure
        # telemetry reads as "book so far + this proposed trade" (consistent with every reject path).
        _log_decision("filled", spot=spot, atr=atr, expiry=expiry, order=order)
        state.open_positions.append(ManagedPosition(
            "SPY", order.short_strike, order.long_strike, credit, qty, expiry,
            entry_date=today, opening_fees=opening_fees))
        state.last_entry_date = today
        state.entries_today += 1             # count toward the per-day entry cap
        # NOTE: credit_ratio_history is already appended for EVERY evaluated candidate (including
        # rejects) in the credit-tiers block above (partner review v2 §3) -- no separate append here.
        open_rec = {"event": "OPEN", "date": today, "ticker": "SPY",
                    "short": order.short_strike, "long": order.long_strike, "expiry": expiry,
                    "qty": qty, "credit": credit, "status": status}
        if gap_losses is not None:   # partner review v2 §10: log all three stress scenarios
            open_rec["gap_stress_1_0"] = gap_losses[1.0]
            open_rec["gap_stress_1_5"] = gap_losses[1.5]
            open_rec["gap_stress_2_0"] = gap_losses[2.0]
        deps.trade_log(open_rec)
    else:
        # order submitted but did not fill (e.g. timeout/rejected at the broker) -- still a return
        # path that must be decision-logged (spec §1: EVERY decision, not just the happy path).
        _log_decision(status, spot=spot, atr=atr, expiry=expiry, order=order)
    return state, status


def tick(state: BotState, deps: Deps, now) -> BotState:
    """One bot cycle: compute regime -> reconcile (may halt) -> manage -> de-gross -> enter if eligible.
    The regime is computed UP FRONT so PHASE 1's trend gate can pause entries and PHASE 1.5 can
    de-gross held positions in a confirmed downtrend; it is still shadow-logged. A regime fault returns
    None (no gate, no de-gross) and never halts trading."""
    today = now.strftime("%Y-%m-%d")
    regime = None
    if deps.regime_provider is not None:
        try:
            regime = deps.regime_provider()
        except Exception as exc:
            deps.alert_sink([Alert(Severity.INFO, f"regime unavailable: {exc}")])
            regime = None
    state, reconcile_ok = run_reconcile_cycle(state, deps)
    state, _ = run_management_cycle(state, deps, today)   # ALWAYS runs (stops must fire)
    state, _ = run_degross_cycle(state, deps, today, regime)  # PHASE 1.5: ALWAYS runs (risk reduction)
    state, _ = run_flow_degross_cycle(state, deps, today, regime)  # flow-flip de-gross (gated, off by default)
    if reconcile_ok:
        state, _ = run_entry_cycle(state, deps, now, regime)
    # §13 shadow monitor: ALWAYS runs (regardless of whether an entry happened, or even whether
    # reconcile succeeded) when the feature is on -- it only reads state/logs, never gates entry.
    run_shadow_monitor_cycle(state, deps, today)
    # §14 research markouts: ALWAYS runs (same rationale as the shadow monitor above) when the
    # feature is on -- advances due horizons / writes the EOD row; never gates entry.
    run_markout_cycle(state, deps, now)
    if regime is not None:
        try:
            deps.regime_log(regime, now.isoformat(), "TICK")
        except Exception:
            pass
    # remember this tick's flow bias so the NEXT tick can detect a bull->bear flip (flow-degross)
    if regime is not None:
        state.prev_flow_bias = getattr(regime, "flow_bias", state.prev_flow_bias)
    return state
