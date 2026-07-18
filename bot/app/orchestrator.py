"""Bot orchestrator: tick = reconcile -> manage -> enter, with halt-gating (spec §3,4,5,7)."""
from dataclasses import dataclass, field, replace

from bot.sizing import contracts_for_risk, regime_adjusted_risk_pct
from bot.risk_gate import RiskGate, RiskConfig
from bot.strategy.s2b import build_spread_order, S2bConfig, to_tradier_payload, _occ
from bot.strategy.execution_price import (quotes_valid, package_too_wide, package_mid,
                                           package_natural, expected_executable_credit,
                                           spot_moved_too_far)
from bot.features import S2bFeatures
from bot.broker.order_state import result_status
from bot.strategy.manage import (monitor_positions, ManageConfig, ManagedPosition, ExitAction,
                                  dte_from_expiry)
from bot.strategy.credit_quality import (dte_bucket, full_size_threshold, _percentile,
                                          should_record_observation, classify_credit_quality,
                                          candidate_key, record_candidate, bucket15_of,
                                          CreditObservation,
                                          low_credit_safety_pass)
from bot.strategy.cost_gate import cost_gate_eval
from bot.strategy.quote_quality import (spread_quote_age_seconds, calculate_quote_age_seconds,
                                         quote_freshness_pass, calculate_atm_iv,
                                         expected_move_to_expiry, expected_move_cushion_of)
from bot.portfolio.risk_budget import (size_qty, remaining_stop_risk,
                                        planned_stop_loss_per_contract, structural_max_loss_per_contract,
                                        apply_quality_multiplier, budget_quantity_caps,
                                        size_to_risk_limits, classify_outcome, DecisionOutcome)
from bot.portfolio.gap_stress import gap_stress_losses, gap_quantity_caps
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
    credit_obs_last: dict = field(default_factory=dict)   # DTE-bucket -> last RECORDED observation dict
                                                            # {date,expiry,short,long,ratio,bucket15}, used by
                                                            # should_record_observation to dedup repeated
                                                            # near-identical candidates within a polling day
                                                            # (partner review v2 item 3 fix)
    entry_cycles_started: int = 0                         # advisor Decision 2: EVERY scheduled entry cycle,
                                                            # counted at the very top -- including cycles that
                                                            # exit at max_open/max_entries before a candidate
                                                            # is ever constructed. The gap between this and
                                                            # raw_candidate_evaluations is exactly "how much of
                                                            # the session was spent blocked by position limits".
    seen_candidate_keys: dict = field(default_factory=dict)  # post-v2 refinement §12: candidate_key ->
                                                            # last-seen credit ratio (the anchor). A dict
                                                            # rather than the spec's bare set because the
                                                            # ">= 0.005 ratio move re-counts" rule needs the
                                                            # prior ratio per key. Pruned to the current
                                                            # trading date on each record_candidate call;
                                                            # round-trips via state_store (tuple keys are
                                                            # encoded as lists for JSON).
    raw_candidate_evaluations: int = 0                    # §12/§17: incremented once a valid candidate spread
                                                            # has actually been CONSTRUCTED (diagnostics only --
                                                            # "do not use raw polling evaluations in
                                                            # profitability reports")
    unique_candidate_opportunities: int = 0               # §12/§17: materially distinct opportunities
    realized_today: float = 0.0                           # sum of realized P&L from closes on `risk_day` (partner review v2 §11)
    risk_day: str = ""                                    # the calendar date `realized_today` applies to
    markout_pending: list = field(default_factory=list)   # persisted MarkoutTracker pending state
                                                           # (partner review v2 §14); round-trips
                                                           # via state_store. Only ever populated
                                                           # when deps.features.markout_tracking
                                                           # is on -- stays [] forever for Bot C.
    markout_seq: int = 0                                  # monotonic counter -> unique markout signal_ids
    markout_obs_last: dict = field(default_factory=dict)  # last RECORDED markout candidate key
                                                            # {date,expiry,short,long,bucket15}; used to
                                                            # dedup repeated identical candidates polled
                                                            # many times in one 15-min window so they do
                                                            # not each spawn a pending markout (partner
                                                            # review v2 item 4). Only touched when
                                                            # markout_tracking is on -> stays {} for Bot C.

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
                                             # trade log. ONLY called when deps.features.markout_tracking
                                             # is on; best-effort, wired for real in bot.app.wiring.build_deps.
    option_quotes: callable = None           # (list[occ_symbol]) -> {occ_symbol: mid_price}; ONE batched
                                             # Tradier /markets/quotes fetch used by run_markout_cycle's
                                             # resolve_due_batched to price all due-markout legs off the
                                             # critical tick() path in a single call (Priority-0 fix item 4).
                                             # None -> run_markout_cycle falls back to the per-item
                                             # deps.mark_position loop. ONLY invoked when markout_tracking
                                             # is on, so the live Bot C never calls it (zero new API traffic).


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
    # Task 2 (advisor-mandated PARTIAL-FILL accounting): a close may fill only SOME of a position's
    # contracts. On the Bot C path such a partial surfaces as failed=True with a NON-"filled" status
    # ("canceled"/"timeout"), yet close_result.filled_quantity > 0 -- so we CLASSIFY off
    # filled_quantity, never the status string. The submit layer has ALREADY cancelled/left the
    # unfilled remainder (both the ladder and non-ladder paths cancel it themselves), so we NEVER
    # cancel here. A partial that filled SOME is PROGRESS, not a stuck stop: we book realized P&L on
    # the filled contracts, reduce the position's qty by that amount, and KEEP the remainder under
    # management -- it must NOT trip the "failed close" halt (only a close that filled NOTHING does).
    closed_ok = set()          # positions whose close FULLY filled -> removed from the book
    hard_failed = []           # cfq == 0 STOP/TIME_EXIT/ERROR closes (genuine stuck closes) -> alert + halt
    nonhalting_alerts = []     # non-halting WARNs (never silent, never halt): a partial-with-progress,
                               # and a cfq == 0 TAKE_PROFIT (winning position, WARN + retry, not a halt)
    for r in results:                                    # per-bot trade log (for A/B measurement)
        cfq = getattr(r.close_result, "filled_quantity", 0) or 0
        # A status-"filled" close is a FULL close (byte-identical to before). A failed close that
        # nonetheless reports cfq >= the tracked qty is ALSO treated as full (defensive: a
        # "shouldn't happen" broker state we never want to leave tracked). Anything strictly between
        # (0 < cfq < qty) is a genuine partial. A cfq == 0 failed close then splits three ways below:
        # a failed TAKE_PROFIT warns + retries (no halt), while STOP/TIME_EXIT/ERROR are the real
        # stuck closes that halt.
        full_close = (not r.failed) or (cfq >= r.position.qty)
        partial_close = (not full_close) and cfq > 0
        # ONLY a genuine partial logs cfq; full closes AND hard fails (nothing filled) log the full
        # position qty. Keying off partial_close (not `full_close else cfq`) keeps a ZERO-fill stuck
        # close's CLOSE-row qty byte-identical to pre-Task-2 (cfq==0 would otherwise regress it to 0);
        # book_qty for a hard fail is used ONLY in the CLOSE rec qty field (the P&L block is skipped).
        book_qty = cfq if partial_close else r.position.qty
        # Fix 1: prorate the position's opening_fees by the fraction of contracts closed on THIS fill,
        # so a partial close deducts only its share and the surviving remainder keeps the rest (see the
        # partial_close branch, which decrements the stored fees by exactly this amount). Across any
        # number of partials plus the final full close this telescopes back to the ORIGINAL fee, with
        # no double-count. r.position.qty here is the PRE-reduction qty (the correct denominator); for
        # a full close book_qty == qty so opening_fees_booked == opening_fees -> byte-identical to today.
        opening_fees_booked = round(r.position.opening_fees * book_qty / r.position.qty, 2)
        rec = {"event": "CLOSE", "date": today, "ticker": r.position.ticker,
               "short": r.position.short_strike, "long": r.position.long_strike,
               "expiry": r.position.expiry, "qty": book_qty, "credit": r.position.credit,
               "action": r.action.value, "exit_value": r.value, "status": r.close_status}
        if (full_close or partial_close) and r.value is not None:
            if deps.features.actual_fill_accounting:
                # §8: realized P&L must use the ACTUAL close fill, never the triggering mark;
                # report BOTH gross and net (net subtracts opening + closing fees/commissions).
                actual_close = getattr(r.close_result, "average_fill_price", None)
                close_value = actual_close if actual_close is not None else r.value
                gross_pnl = round((r.position.credit - close_value) * 100 * book_qty, 2)
                close_commissions = getattr(r.close_result, "commissions", 0.0) or 0.0
                close_reg_fees = getattr(r.close_result, "regulatory_fees", 0.0) or 0.0
                net_pnl = round(gross_pnl - opening_fees_booked
                                - close_commissions - close_reg_fees, 2)
                rec["gross_pnl"] = gross_pnl
                rec["net_pnl"] = net_pnl
                rec["pnl"] = net_pnl                       # the bottom-line number, now cost-aware
            else:
                # flag OFF -> byte-identical to before: (credit - triggering mark) * 100 * qty
                rec["pnl"] = round((r.position.credit - r.value) * 100 * book_qty, 2)
            _accumulate_realized(state, today, rec["pnl"])   # partner review v2 §11 (always on)
        deps.trade_log(rec)
        if full_close:
            closed_ok.add(position_key(r.position))
        elif partial_close:
            # reduce the tracked qty to the still-working remainder and KEEP the position; the submit
            # layer already cancelled the balance, so there is nothing to cancel here. Mutating qty is
            # safe: position_key keys on (ticker,short,long,expiry), so the open_positions rebuild
            # below keeps this same object at its reduced qty. Fix 1: decrement the stored opening_fees
            # by exactly the prorated share booked above, so the remainder carries only its unbooked
            # portion and the fee is never double-counted when that remainder later closes.
            r.position.qty -= cfq
            r.position.opening_fees = round(r.position.opening_fees - opening_fees_booked, 2)
            nonhalting_alerts.append(Alert(Severity.WARN,
                f"partial close ({r.action.value}) for {r.position.ticker} "
                f"{r.position.short_strike}/{r.position.long_strike}: filled {cfq}, "
                f"remainder {r.position.qty} still working"))
        elif r.action == ExitAction.TAKE_PROFIT:
            # A failed TAKE-PROFIT is NOT a risk event (the position is winning; we just didn't
            # capture profit this tick). It must NOT set the sticky "failed close" halt -- that
            # was the root cause of Bot C's recurring $1-wing halt. WARN and let the next
            # management cycle retry the close (re-priced at fresh natural). Only STOP/TIME_EXIT/
            # ERROR (real must-exit risk) still halt (fall through to hard_failed below).
            nonhalting_alerts.append(Alert(Severity.WARN,
                f"take-profit close did not fill ({r.close_status}) for {r.position.ticker} "
                f"{r.position.short_strike}/{r.position.long_strike}: qty {r.position.qty} kept, will retry"))
        else:
            hard_failed.append(r)     # STOP / TIME_EXIT / ERROR that filled nothing -> alert + halt
    # Only a close that filled NOTHING is a "failed close" that can halt new entries. A partial that
    # filled SOME is surfaced as a (non-halting) WARN so it is never silent but never trips the halt.
    alerts = alerts_for_cycle(hard_failed, drift_report=None) + nonhalting_alerts
    if alerts:
        deps.alert_sink(alerts)
    # remove only positions whose close FULLY filled; partials stay (at reduced qty), hard fails stay
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

    Batching (Priority-0 fix item 4): when deps.option_quotes is wired, all due-markout leg quotes
    are priced in ONE batched broker call via resolve_due_batched -- cheap and off the critical
    path -- instead of one deps.mark_position quote call per pending item. When option_quotes is
    None the per-item deps.mark_position fallback preserves the original behavior.

    LOG ONLY: reads/writes only state.markout_pending via MarkoutTracker; never touches an order,
    a size, or any trading decision. No-op unless deps.features.markout_tracking is on (Bot C:
    always off). Best-effort: any failure (a down feed, a bad quote) is swallowed -- a markout
    logging failure must never affect trading, and must never raise out of tick()."""
    if not deps.features.markout_tracking:
        return
    try:
        tracker = MarkoutTracker.from_state(state.markout_pending, deps.markout_log)

        def _spread_value(item):
            pos = ManagedPosition(item["ticker"], item["short_strike"], item["long_strike"],
                                  credit=0.0, qty=1, expiry=item["expiry"])
            return deps.mark_position(pos)

        spy_now = deps.get_spot("SPY")
        if deps.option_quotes is not None:
            # ONE batched quote call for every due-markout leg, off the critical path.
            tracker.resolve_due_batched(now, spy_now, deps.option_quotes)
        else:
            tracker.resolve_due(now, spy_now, _spread_value)   # per-item fallback (unwired)
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


def _chain_quote(chain, strike, nearest=False):
    """The OptionQuote at `strike`, or None. With nearest=True return the closest strike instead of
    requiring an exact match (used to find the at-the-money leg for the expected-move IV)."""
    if not chain:
        return None
    if nearest:
        return min(chain, key=lambda q: abs(q.strike - strike))
    return next((q for q in chain if q.strike == strike), None)


def run_entry_cycle(state: BotState, deps: Deps, now, regime=None) -> tuple:
    today = now.strftime("%Y-%m-%d")
    state.entry_cycles_started += 1      # advisor Decision 2, stage 1/3: count the CYCLE itself, before any
                                          # gate can return -- so a session spent entirely at the position
                                          # limit is distinguishable from one that evaluated and rejected.
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
                                    # set once the aggregate_risk_budget block runs and caps to 0.
                                    # Task 5 retired its population (the quantity-cap pipeline emits
                                    # risk_sizing_telemetry instead); the fields stay declared in
                                    # wiring._DECISION_LOG_FIELDS for CSV back-compat but go unfilled.
    risk_sizing_telemetry = None   # Task 5 (§10/§11): the quantity-cap sizing outcome -- limiting_gate
                                    # + requested/quality-adjusted/final qty + the binding cap's
                                    # exposure/limit/remaining/incremental; set once the sizing runs.
    decision_outcome_val = None    # Task 5 (§11): the DecisionOutcome label (allowed_full/reduced,
                                    # blocked_zero_capacity, blocked_credit_quality); set at each
                                    # risk-sizing / credit-quality decision point.
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
        gated on markout_tracking (NOT decision_logging) so markouts are captured even when
        decision logging itself is off. LOG ONLY: only ever appends to state.markout_pending;
        NEVER touches an order/size/decision. Best-effort -- never raises out of run_entry_cycle.

        Dedup (Priority-0 fix item 4): a bot polling every few minutes re-evaluates the SAME
        candidate spread dozens of times a day; without dedup each poll would spawn its own pending
        markout, flooding the research log with near-duplicate follow-ups of one signal. Reusing the
        item-3 dedup notion, we key on (date, expiry, short, long, bucket15, filled) and skip
        recording when this candidate matches the last one recorded -- so repeated identical
        candidates in one 15-minute window spawn at most one pending markout; a new window or a new
        strike spawns another. `filled` is part of the key on purpose: a candidate that is rejected
        and then FILLED at the same strikes in the same window must still capture the fill's richer
        follow-up (filled=True carries tp_value/stop_value and drives time-to-TP/time-to-stop) -- the
        reject->fill transition is exactly the filled-vs-rejected comparison §14 exists to measure."""
        try:
            # Post-v2 refinement §13 alignment: same clock-quarter-hour window as the §12 candidate
            # dedup and the credit-history dedup. This block's whole premise is "reusing the item-3
            # dedup notion", so it must not keep a third, different definition of a 15-minute window.
            bucket15 = bucket15_of(now)          # `now` is ET
            filled = (reason == "filled")
            key = {"date": today, "expiry": expiry, "short": order.short_strike,
                   "long": order.long_strike, "bucket15": bucket15, "filled": filled}
            if state.markout_obs_last == key:
                return                       # same candidate+outcome already recorded this 15-min window
            state.markout_seq += 1
            tracker = MarkoutTracker.from_state(state.markout_pending, deps.markout_log)
            cfg = deps.manage_cfg
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
            state.markout_obs_last = key     # remember this candidate for the next poll's dedup
        except Exception:
            pass

    def _log_decision(reason, spot=None, atr=None, expiry=None, order=None):
        """§1/§12: write a DECISION record (reason + active flags + best-effort exposure telemetry)
        to the trade log. No-op unless deps.features.decision_logging is on; never raises.

        Also independently triggers the §14 research markout record (_record_markout) whenever
        markout_tracking is on and a concrete candidate (order) exists -- that piece runs
        regardless of decision_logging, since the two flags are orthogonal."""
        if deps.features.markout_tracking and order is not None:
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
        if risk_sizing_telemetry is not None:   # Task 5: quantity-cap sizing outcome + binding gate
            rec.update(risk_sizing_telemetry)
        if decision_outcome_val is not None:    # Task 5: §11 decision-outcome label
            rec["decision_outcome"] = decision_outcome_val
        rec.update(_decision_telemetry(state, deps, today, spot=spot, atr=atr, expiry=expiry, order=order,
                                        foreign_positions=foreign_position_list, iv_fn=iv_fn,
                                        prime_iv=_prime_leg_ivs))
        try:
            deps.trade_log(rec)
        except Exception:
            pass

    def _record_open(*, decision_reason, order, expiry, status, qty, credit, opening_fees,
                     gap_losses, spot, atr):
        """Fold a newly-opened position into the book and emit its OPEN row -- SHARED by the full-fill
        and partial-fill branches so a future OPEN-row/telemetry field can never silently drift onto
        only one path (this is the real-money order path). Each caller computes its own qty/credit/
        opening_fees/decision reason AND the OPEN-row `status` cell (the only real deltas), then hands
        them here. The DECISION log runs BEFORE the append, so the exposure telemetry reads as "book
        so far + this proposed trade" (consistent with every reject path).

        A partial open is disambiguated purely via the `status` field the caller passes ("partial_fill"
        instead of the raw broker "canceled"/"timeout") -- NOT a separate "partial" column: the live
        trade CSV is pinned to exactly 12 fields (_LOG_FIELDS) and DictWriter(extrasaction="ignore")
        would silently drop any extra key, so a boolean flag would never reach trades_live.csv."""
        _log_decision(decision_reason, spot=spot, atr=atr, expiry=expiry, order=order)
        state.open_positions.append(ManagedPosition(
            "SPY", order.short_strike, order.long_strike, credit, qty, expiry,
            entry_date=today, opening_fees=opening_fees))
        state.last_entry_date = today
        state.entries_today += 1             # count toward the per-day entry cap
        open_rec = {"event": "OPEN", "date": today, "ticker": "SPY",
                    "short": order.short_strike, "long": order.long_strike, "expiry": expiry,
                    "qty": qty, "credit": credit, "status": status}
        if gap_losses is not None:   # partner review v2 §10: log all three stress scenarios
            open_rec["gap_stress_1_0"] = gap_losses[1.0]
            open_rec["gap_stress_1_5"] = gap_losses[1.5]
            open_rec["gap_stress_2_0"] = gap_losses[2.0]
        deps.trade_log(open_rec)

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
    # Post-v2 refinement §12: count this evaluation BEFORE any sizing/credit gate can reject it, so
    # the opportunity statistics cover every candidate the bot actually looked at (§17 reports
    # rejections BY gate, which only reconciles if the denominator counts pre-rejection). Two
    # counters: raw_polling_evaluations (every look) and unique_candidate_opportunities (materially
    # distinct ones -- new strike pair / expiry / 15-min bucket, or a >= 0.005 credit-ratio move).
    # Unconditional (not behind credit_tiers): this is pure REPORTING state, it gates nothing, and
    # opportunity counts are exactly as meaningful on a bot with tiering off. `now` is ET.
    # Stages 2/3 and 3/3 of the advisor's Decision 2 counter split (stage 1/3 is at the top of this
    # function). We are past the point where a valid candidate spread has been CONSTRUCTED.
    state.raw_candidate_evaluations += 1
    _cand_credit = exec_credit if exec_credit is not None else order.credit
    _cand_key = candidate_key(today, expiry, order.short_strike, order.long_strike, now)
    if record_candidate(key=_cand_key, ratio=round(_cand_credit / deps.s2b_cfg.wing_width, 4),
                        seen=state.seen_candidate_keys, trading_date=today):
        state.unique_candidate_opportunities += 1
    # Adaptive credit-quality tiering (partner review v2 §3), OPT-IN via deps.features.credit_tiers
    # (False = feature OFF -> this entire block is a no-op, so default behavior is byte-identical to
    # before). Sizes off exec_credit (the conservative expected-executable credit computed by the §4
    # guards above, when available -- falls back to order.credit if quote guards were skipped)
    # rather than the naive mid-quote credit. A credit too thin relative to the wing width is
    # rejected outright; a mid-tier credit is sized down to a "probe"; a strong credit trades full
    # size (see bot/strategy/credit_quality.py).
    quality_multiplier = 1.0
    quality_maximum_qty = None   # Post-v2 refinement §1: the low_credit_safety lane caps qty at 1;
                                  # None otherwise (probe/full). Consumed by apply_quality_multiplier
                                  # (aggregate path) AND the legacy else-branch min() (aggregate OFF).
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
        # §3: "Use prior candidate signals, including rejected signals" -- append EVERY MATERIALLY
        # DISTINCT evaluated candidate (even one about to be rejected below) so future thresholds see
        # a genuine population of opportunities, not just fills. Priority-0 fix item 3: a bot polling
        # every few minutes re-evaluates the SAME spread dozens of times a day; without dedup, one
        # ordinary day (e.g. 240 cycles) overwrites the entire 60-signal window with near-duplicate
        # observations of a single candidate, degrading the adaptive threshold into a polling-
        # frequency indicator. should_record_observation gates the append on the candidate being new
        # (day/expiry/strike), in a new 15-minute research window, or having moved >= 0.5pp in ratio.
        # NOTE: `prior`/`thr` above are computed from the copy captured BEFORE this append -- that
        # ordering is unchanged, so there is still no look-ahead regardless of whether this candidate
        # gets recorded.
        # Post-v2 refinement §13: the research window is now the SAME one §12's candidate_key uses
        # (clock quarter-hours, hour-qualified) instead of the old minutes-since-open count, so the
        # two dedup rules can't drift apart. NOTE: the first tick after deploy sees a bucket15 that
        # doesn't match the persisted pre-deploy value and records one extra observation -- a
        # one-off, harmless against the 60-entry window. `now` is ET (see run_entry_cycle docstring).
        bucket15 = bucket15_of(now)
        ratio_r = round(ratio, 4)   # single rounded value shared by the dedup key, the append, and the anchor
        obs = {"date": today, "expiry": expiry, "short": order.short_strike, "long": order.long_strike,
               "ratio": ratio_r, "bucket15": bucket15}
        if should_record_observation(state.credit_obs_last.get(bucket), date=today, expiry=expiry,
                                      short=order.short_strike, long=order.long_strike,
                                      ratio=ratio_r, bucket15=bucket15):
            hist = state.credit_ratio_history.setdefault(bucket, [])
            hist.append(ratio_r)                              # STILL a bare float (advisor Decision 1,
            state.credit_ratio_history[bucket] = hist[-60:]   # Option B: no persisted-state migration)
            state.credit_obs_last[bucket] = obs
            # Advisor Decision 1 (Option B): emit the structured audit record to the LOG only. Gated
            # by the same dedup rule as the append, so the audit trail mirrors the rolling history
            # one-for-one instead of becoming a per-poll firehose.
            if deps.trade_log is not None:
                deps.trade_log(CreditObservation(
                    timestamp=now, expiry=expiry, dte_bucket=bucket,
                    short_strike=order.short_strike, long_strike=order.long_strike,
                    expected_executable_credit=round(credit_for_tier, 4), credit_ratio=ratio_r,
                    candidate_key=_cand_key,
                ).log_fields())
        # Post-v2 refinement §1: classify_credit_quality REPLACES the old credit_tier multiplier --
        # four lanes (reject / low_credit_safety / probe / full), returning the size multiplier AND a
        # maximum_quantity (the low-credit lane caps at 1). This is the credit classifier on the
        # credit-tiers path REGARDLESS of aggregate_risk_budget, so the low_credit_safety lane is live
        # wherever credit_tiers is on (spec §1). ABSOLUTE_CREDIT_FLOOR (0.08) is the hard reject floor
        # now, NOT min_credit_ratio (0.10): the [0.08, 0.10) band is a low_credit_safety lane,
        # tradeable at quarter size ONLY if the strict low_credit_safety_pass gate holds (NOT loosened).
        tier, quality_mult, quality_max = classify_credit_quality(ratio, thr)
        credit_telemetry = {
            "credit_ratio": ratio,
            "credit_pctl40": (_percentile(prior, 40) if count else None),
            "credit_sample_count": count,
            "credit_threshold": thr,
            "credit_quality_mult": quality_mult,
        }
        quality_multiplier = quality_mult
        quality_maximum_qty = quality_max
        if tier == "reject":
            decision_outcome_val = DecisionOutcome.BLOCKED_CREDIT_QUALITY.value
            _log_decision("credit_too_low", spot=spot, atr=atr, expiry=expiry, order=order)
            return state, "credit_too_low"
        if tier == "low_credit_safety":
            # Additional hard gate for the 0.08-0.10 band (spec §1). INPUT WIRING + availability
            # caveats (3 available, 2 derivable, 2 unplumbed) -- documented here so the approximations
            # are auditable on this real-money path:
            #   target_to_cost_ratio -- cost_gate_eval on the conservative credit (computed HERE even
            #     if the transaction_cost_gate flag path did not run);
            #   package_width_ratio  -- the SAME scalar package_too_wide thresholds:
            #     (package_mid - package_natural) / max(package_mid, 0.01), from the leg quotes;
            #   cushion_atr          -- (spot - short_strike) / atr;
            #   short_delta          -- entry_delta (best-effort chain delta; None => FAIL CLOSED);
            #   quote_age_seconds    -- advisor Step 1A: the age of the OLDER leg, from the feed's
            #     exchange quote timestamps. None (unknown) fails closed -- the current time is NEVER
            #     substituted, since that would make stale cached data read as perfectly fresh;
            #   expected_move_cushion-- advisor Step 1B: (spot - short_strike) / one-sigma expected
            #     move to THIS candidate's expiry, from ATM IV on the same expiry. None when IV is
            #     unavailable, in which case the lane falls back to the OR branch (short_delta<=0.30);
            #   defensive_market_state - regime.trend_regime == "risk_off".
            ttc = cost_gate_eval(credit_for_tier, deps.features)["target_to_cost_ratio"]
            short_q = _chain_quote(chain, order.short_strike)
            long_q = _chain_quote(chain, order.long_strike)
            quote_age = (spread_quote_age_seconds(short_q, long_q, now)
                         if short_q is not None and long_q is not None else None)
            # ATM IV on the SAME expiry as the candidate (spec: "use the same expiration"). The chain
            # this bot parses is puts-only, so the put side supplies the vol -- which the rule
            # explicitly permits ("use the valid side if only one is available"). Near ATM the
            # call/put vols are close enough that the put alone is a sound stand-in; if calls are
            # ever parsed, pass the call IV here and the average takes over automatically.
            atm_q = _chain_quote(chain, spot, nearest=True)
            atm_iv = calculate_atm_iv(None, atm_q.iv if atm_q is not None else None)
            exp_move = expected_move_to_expiry(spot, atm_iv, dte_from_expiry(expiry, today))
            em_cushion = expected_move_cushion_of(spot=spot, short_strike=order.short_strike,
                                                   expected_move=exp_move)
            if order.short_bid is not None:
                pm = package_mid(order.short_bid, order.short_ask, order.long_bid, order.long_ask)
                pn = package_natural(order.short_bid, order.short_ask, order.long_bid, order.long_ask)
                pkg_width_ratio = (pm - pn) / max(pm, 0.01)
            else:
                pkg_width_ratio = float("inf")   # no leg quotes -> fail the width condition (closed)
            cushion_atr = (spot - order.short_strike) / atr if atr and atr > 0 else 0.0
            defensive = (regime is not None
                         and getattr(regime, "trend_regime", "unknown") == "risk_off")
            passed = low_credit_safety_pass(
                target_to_cost_ratio=ttc,
                package_width_ratio=pkg_width_ratio,
                quote_age_seconds=quote_age,
                cushion_atr=cushion_atr,
                expected_move_cushion=em_cushion,
                short_delta=entry_delta,
                defensive_market_state=defensive,
            )
            # Advisor Step 1A: log the freshness inputs so a lane decision is auditable after the
            # fact -- which leg was stale, and whether the timestamp came from the feed at all.
            credit_telemetry.update({
                "lc_short_quote_age": (calculate_quote_age_seconds(short_q, now)
                                       if short_q is not None else None),
                "lc_long_quote_age": (calculate_quote_age_seconds(long_q, now)
                                      if long_q is not None else None),
                "lc_spread_quote_age": quote_age,
                "lc_quote_time_source": ("exchange" if (short_q is not None
                                                        and short_q.exchange_timestamp is not None)
                                          else "received" if (short_q is not None
                                                              and short_q.received_timestamp is not None)
                                          else "none"),
                "lc_quote_fresh": quote_freshness_pass(quote_age),
                "lc_atm_iv": atm_iv,
                "lc_expected_move": exp_move,
                "lc_expected_move_cushion": em_cushion,
                "lc_safety_pass": passed,
            })
            if not passed:
                decision_outcome_val = DecisionOutcome.BLOCKED_CREDIT_QUALITY.value
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
        wing = deps.s2b_cfg.wing_width
        # Post-v2 refinement §2 "Correct sequence" (REDUCE-not-reject): base qty -> quality qty ->
        # cap to ALL risk limits, taking the strictest. size_qty with quality_multiplier=1.0 returns
        # the pre-quality BASE (min(entry-stop, structural)); apply_quality_multiplier then folds in
        # the credit-quality multiplier AND the low-credit maximum (a probe no longer floors a valid
        # base to 0; the low_credit_safety lane caps at 1). That quality qty flows into
        # size_to_risk_limits against the four aggregate budgets (budget_quantity_caps) PLUS the three
        # gap-stress scenarios (gap_quantity_caps, spec §8/§9) -- so an oversized request is SIZED DOWN
        # to the binding cap and PROCEEDS; only final_qty <= 0 rejects. This REPLACES the old
        # cap_to_budgets int + gap-stress shrink-loop (both deleted; gap is now just another cap).
        base_qty = size_qty(req, risk_credit, wing, deps.features, quality_multiplier=1.0)
        quality_qty = apply_quality_multiplier(base_qty, quality_multiplier, quality_maximum_qty)
        # The gap-stress book mirrors the budget caps' foreign inclusion (Priority-0 fix item 6):
        # foreign SPY spreads at the broker must be stressed too (budget caps get foreign via
        # foreign_exposure=foreign; the gap book must carry foreign_position_list to match). Gap caps
        # are BS-only (spec §9: "Do not use intrinsic value alone" -- gap_quantity_caps always reprices
        # via Black-Scholes; f.gap_stress_model is vestigial for ENFORCEMENT, both live bots run "bs").
        # Prime the batched, memoized real-greeks fetch once for the whole book -- but ONLY when a
        # positive quality qty is actually going to be sized (mirrors the old no-fetch-on-zero path, so
        # a candidate that rounds to 0 before any book budget is consulted still triggers no fetch).
        gap_book_open = state.open_positions + foreign_position_list
        candidate_view = replace(order, credit=risk_credit)   # proposed leg stressed at the
                                                              # conservative credit; qty irrelevant here
                                                              # (gap_quantity_caps stresses 1 contract)
        if quality_qty > 0:
            _prime_leg_ivs(gap_book_open + [candidate_view])
            caps = (budget_quantity_caps(risk_credit, wing, expiry, today, state.open_positions,
                                         deps.mark_position, req, deps.features, foreign_exposure=foreign)
                    + gap_quantity_caps(gap_book_open, candidate_view, spot, atr, deps.features, req,
                                        iv_fn=iv_fn, today=today))
        else:
            caps = []
        result = size_to_risk_limits(quality_qty, caps)
        final_qty = result.final_qty
        # Telemetry (spec §10/§11): name the binding-or-tightest cap and carry its exposure numbers.
        binding_cap = next((c for c in result.caps if c.name == result.limiting_gate), None)
        risk_sizing_telemetry = {
            "limiting_gate": result.limiting_gate,
            "requested_qty": quality_qty,
            "quality_adjusted_qty": quality_qty,
            "final_qty": final_qty,
            "risk_current_exposure": (round(binding_cap.current_exposure, 2)
                                      if binding_cap is not None else None),
            "risk_limit": round(binding_cap.limit, 2) if binding_cap is not None else None,
            "risk_remaining_capacity": (round(binding_cap.remaining_capacity, 2)
                                        if binding_cap is not None else None),
            "risk_incremental_per_contract": (round(binding_cap.incremental_risk_per_contract, 2)
                                              if binding_cap is not None else None),
        }
        if final_qty <= 0:
            # Reduce-not-reject still rejects a genuinely full book. Return the BARE "risk_budget" info
            # string (unchanged from before, so callers keying on it are unaffected); the SPECIFIC gate
            # lives in the logged reason f"risk_budget:{limiting_gate}" + the limiting_gate telemetry.
            # When quality_qty was already 0 (limiting_gate == "requested_qty", set by size_to_risk_limits
            # before any cap is consulted), log the bare "risk_budget" -- no cap can be blamed.
            decision_outcome_val = DecisionOutcome.BLOCKED_ZERO_CAPACITY.value
            reason = ("risk_budget" if result.limiting_gate in (None, "requested_qty")
                      else f"risk_budget:{result.limiting_gate}")
            _log_decision(reason, spot=spot, atr=atr, expiry=expiry, order=order)
            return state, "risk_budget"
        order.qty = final_qty
        decision_outcome_val = classify_outcome(quality_qty, final_qty).value   # allowed_full/reduced
        # OPEN-row gap telemetry (spec §10): the ENFORCED gap-stress losses at the FINAL qty. Reuse
        # gap_stress_losses (which respects f.gap_stress_model); under the default "bs" (both live bots)
        # this equals the BS-enforced caps, so "logged == enforced" holds for the real configs. The
        # book mirrors the caps' book (own + foreign + proposed); iv_fn/today already primed above.
        gap_losses = gap_stress_losses(gap_book_open + [replace(order, credit=risk_credit)],
                                       spot, atr, wing, today=today, iv_fn=iv_fn, f=deps.features)
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
            decision_outcome_val = None   # not a risk-sizing outcome; don't carry a stale allowed_*
            _log_decision("daily_risk", spot=spot, atr=atr, expiry=expiry, order=order)
            return state, "daily_risk"
        # ALSO halt new entries (but never management/closing) once today's total P&L (realized +
        # unrealized on every currently-open position) breaches the daily loss-halt threshold.
        unrealized_today = sum((p.credit - deps.mark_position(p)) * 100 * p.qty
                                for p in state.open_positions)
        if state.realized_today + unrealized_today <= -deps.features.daily_pnl_halt_pct * req:
            decision_outcome_val = None   # halt reason, not a risk-sizing outcome
            _log_decision("day_loss_halt", spot=spot, atr=atr, expiry=expiry, order=order)
            return state, "day_loss_halt"
    else:
        order.qty = contracts_for_risk(acct.equity, order.max_loss_per_contract, risk)
        if deps.features.credit_tiers:
            order.qty = max(1, int(order.qty * quality_multiplier))   # a probe of a 1-lot stays 1
            if quality_maximum_qty is not None:   # the low_credit_safety lane caps at 1 (spec §1) --
                order.qty = min(order.qty, quality_maximum_qty)       # honor it on the legacy path too
    decision = RiskGate(deps.risk_cfg).is_order_allowed(order, acct)
    if not decision.allowed:
        decision_outcome_val = None   # a RiskGate reject is not a risk-sizing outcome
        _log_decision(decision.reason, spot=spot, atr=atr, expiry=expiry, order=order)
        return state, decision.reason
    open_result = deps.open_spread(to_tradier_payload(order, expiry, order.qty))
    status = result_status(open_result)
    # Task 2: gate the partial-fill branch on filled_quantity, NOT the status string -- on the Bot C
    # non-ladder path a partial whose remainder got cancelled surfaces as status "canceled"/"timeout"
    # (see wiring._to_execution_result), NOT "partially_filled". A legacy bare-string return has no
    # filled_quantity, so open_filled_qty is 0 and only the status=="filled" full-fill path can fire.
    open_filled_qty = getattr(open_result, "filled_quantity", 0) or 0
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
        # NOTE: credit_ratio_history is already (conditionally, per should_record_observation --
        # item 3 fix) appended for evaluated candidates, including rejects, in the credit-tiers
        # block above (partner review v2 §3) -- no separate append here.
        _record_open(decision_reason="filled", order=order, expiry=expiry, status=status,
                     qty=qty, credit=credit, opening_fees=opening_fees, gap_losses=gap_losses,
                     spot=spot, atr=atr)
    elif open_filled_qty > 0:
        # Task 2 PARTIAL FILL (advisor-mandated): the broker filled SOME but not all of the requested
        # contracts, and the submit layer has ALREADY cancelled/left the remainder (both the ladder
        # and Bot C non-ladder paths cancel the unfilled balance themselves), so we do NOT cancel
        # here. We MUST record the position at the ACTUALLY-filled contract count -- tracking more
        # than the broker filled is the exact bug this fixes (untracked_at_broker at reconcile -> a
        # HALT). qty is ALWAYS the filled quantity, even with actual_fill_accounting OFF (Bot C):
        # this is the one value that must NOT stay at the requested amount. credit/opening_fees follow
        # the Bot C convention (order.credit, no fees) when the flag is off, and use the actual fill
        # price + captured fees only when the flag is on -- mirroring the full-fill branch's pricing,
        # but never overriding qty back up to the requested amount.
        qty = open_filled_qty
        credit, opening_fees = order.credit, 0.0
        if deps.features.actual_fill_accounting:
            fill_price = getattr(open_result, "average_fill_price", None)
            if fill_price is not None:
                credit = fill_price
            commissions = getattr(open_result, "commissions", 0.0) or 0.0
            reg_fees = getattr(open_result, "regulatory_fees", 0.0) or 0.0
            opening_fees = commissions + reg_fees
        # Shared recorder (mirrors the full-fill path exactly, save qty/decision). The OPEN row's
        # `status` cell is the self-describing literal "partial_fill" (NOT the raw broker
        # "canceled"/"timeout"), so a partial open is visible in the 12-column live CSV without adding
        # a column. NOTE: this changes ONLY the logged status; run_entry_cycle still RETURNS the raw
        # broker `status` below, so nothing keying on the return value changes.
        _record_open(decision_reason="partial_fill", order=order, expiry=expiry,
                     status="partial_fill", qty=qty, credit=credit, opening_fees=opening_fees,
                     gap_losses=gap_losses, spot=spot, atr=atr)
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
