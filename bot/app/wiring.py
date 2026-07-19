"""Production wiring: build Deps from Tradier and the runner (spec §6,§7,§8)."""
import csv as _csv
import os as _os

from bot.app import feeds
from bot.app.orchestrator import Deps, tick
from bot.app.state_store import save_state
from bot.strategy.s2b import _occ
from bot.strategy.cost_gate import one_side_commission_per_contract
from bot.features import S2bFeatures

_LOG_FIELDS = ["event", "date", "ticker", "short", "long", "expiry", "qty",
               "credit", "action", "exit_value", "pnl", "status"]
_COST_LOG_FIELDS = ["gross_pnl", "net_pnl"]   # only added when actual_fill_accounting is on (spec §8)
_DECISION_LOG_FIELDS = ["decision", "flags", "positions_today", "positions_in_expiry",
                        "adjacent_strike_distance", "agg_remaining_stop", "agg_structural",
                        "agg_gap_stress_1_5",   # spec §1, §12 exposure telemetry (Task 1.7)
                        "gap_stress_1_0", "gap_stress_1_5", "gap_stress_2_0",   # spec §10 OPEN-row gap-stress scenario losses
                        "expected_executable_credit",   # spec §4 conservative executable credit (Task 2.1)
                        "credit_ratio", "credit_pctl40", "credit_sample_count",
                        "credit_threshold", "credit_quality_mult",   # spec §3 credit-tier telemetry (Task 2.2)
                        "cost_gross_target", "cost_round_trip",
                        "cost_target_ratio",   # spec §5 cost-gate telemetry (Task 2.3)
                        "risk_budget_limiting", "risk_budget_exposure", "risk_budget_limit",
                        "risk_budget_headroom", "risk_budget_proposed_qty",
                        "risk_budget_permitted_qty",   # §9 aggregate-risk-budget attribution (Task 7)
                        "limiting_gate", "requested_qty", "quality_adjusted_qty", "final_qty",
                        "risk_current_exposure", "risk_limit", "risk_remaining_capacity",
                        "risk_incremental_per_contract",
                        "decision_outcome",
                        # Advisor Decision 1 (Option B): CreditObservation audit record fields. The
                        # record shares date/expiry/short/long/credit_ratio with the columns already
                        # declared above; only these three are new. Declared here or
                        # extrasaction="ignore" drops the audit trail on the way to disk.
                        "obs_dte_bucket", "obs_expected_executable_credit", "obs_candidate_key",
                        # Advisor Step 1A/1B: low-credit lane freshness + expected-move inputs, so
                        # a lane decision is auditable (which leg was stale, was IV even available).
                        "lc_short_quote_age", "lc_long_quote_age", "lc_spread_quote_age",
                        "lc_quote_time_source", "lc_quote_fresh", "lc_atm_iv",
                        "lc_expected_move", "lc_expected_move_cushion", "lc_safety_pass",
                        # Advisor Step 2B: intrinsic-vs-Black-Scholes gap comparison (Bot B only).
                        "enforced_model", "gap_loss_limit", "bs_current_book_1_5",
                        "intrinsic_current_book_1_5", "bs_incremental_1_5",
                        "intrinsic_incremental_1_5", "bs_qty_1_5", "intrinsic_qty_1_5",
                        "qty_difference_1_5", "bs_zeroed_the_candidate",
                        # T8 (spec §14): alternate-expiration evaluation + selection.
                        "expirations_evaluated", "expiration_selected", "expiration_outcomes",
                        "no_candidate_fits",
                        # T9 (spec §16): ENTRY_STATE transition records.
                        "previous_state", "new_state", "changed_at", "reason",
                        "unique_candidate_opportunities", "open_positions", "entries_today"]
                                              # spec §10/§11 risk-sizing + decision-outcome telemetry
                                              # (Task 4 declares; Task 5 emits). The risk_budget_*
                                              # fields above stay for back-compat.
                        # only added when decision_logging is on (spec §1, §12); this list must stay
                        # a SUPERSET of every key run_entry_cycle's _log_decision can merge onto a
                        # DECISION record (base rec + _decision_telemetry + credit_telemetry +
                        # cost_telemetry) -- extrasaction="ignore" below silently drops anything
                        # missing here, which is exactly the bug this list fixes (nine fields were
                        # previously dropped from trades_alldays.csv).
_SHADOW_LOG_FIELDS = ["shadow_score", "shadow_action", "spy", "spy_vwap", "spy_atr",
                      "session_high", "session_low", "opening_range_high", "opening_range_low",
                      "qqq_ret", "dia_ret", "soxx_ret", "spy_ret", "qqq_vs_vwap", "soxx_vs_vwap",
                      "vix", "vix1d", "dist_from_vwap_atr", "dist_from_high_atr",
                      "vix1d_vix_ratio", "qqq_minus_dia", "soxx_minus_spy", "put_skew",
                      "short_delta", "short_gamma", "short_iv", "breadth", "up_down_vol",
                      "whale_flow"]   # only added when regime_shadow_monitor is on (spec §13)

# §14 (partner review v2): a wholly SEPARATE research-only CSV -- entry markouts are never mixed
# into the trade log, and never read back into any trading decision.
_MARKOUT_LOG_FIELDS = ["event", "signal_id", "ticker", "short_strike", "long_strike", "expiry",
                      "filled", "entry_spy", "entry_spread_value", "credit", "qty",
                      "entry_delta", "entry_iv", "mfe", "mae", "time_to_tp", "time_to_stop",
                      "mfe_before_stop", "mae_before_tp",
                      "spy_move_1m", "spread_move_1m", "delta_change_1m", "iv_change_1m",
                      "spy_move_5m", "spread_move_5m", "delta_change_5m", "iv_change_5m",
                      "spy_move_15m", "spread_move_15m", "delta_change_15m", "iv_change_15m",
                      "spy_move_30m", "spread_move_30m", "delta_change_30m", "iv_change_30m",
                      "spy_move_60m", "spread_move_60m", "delta_change_60m", "iv_change_60m",
                      "eod_spy_move", "eod_spread_value", "eod_spread_move"]


def make_markout_logger(path):
    """Return a callable that appends one §14 markout record dict to a research-only CSV (header
    written once, lazily on first write -- so the flag-off path, which never calls it, never
    creates the file at all). Wholly separate from make_trade_logger's trade/decision/shadow CSV."""
    def log(record):
        exists = _os.path.exists(path)
        with open(path, "a", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=_MARKOUT_LOG_FIELDS, extrasaction="ignore")
            if not exists:
                w.writeheader()
            w.writerow(record)
    return log


def make_trade_logger(path, include_cost_columns=False, include_decision_columns=False,
                      include_shadow_columns=False):
    """Return a callable that appends a trade-record dict to a CSV (header written once).
    Used per-bot so a shared-account A/B can be measured from separate files.

    include_cost_columns=False (default) keeps the original 12-column CSV shape byte-identical --
    critical for the real-money live bot (run_s2b_live.py), whose actual_fill_accounting is always
    OFF. Pass True only for a bot with the flag on, so its gross_pnl/net_pnl columns are populated.

    include_decision_columns=False (default) likewise keeps the CSV shape byte-identical for the
    live bot (decision_logging always OFF there). Pass True only for a bot with the flag on, so its
    DECISION rows' reason/flags/exposure-telemetry columns are populated.

    include_shadow_columns=False (default) likewise keeps the CSV shape byte-identical for the
    live bot (regime_shadow_monitor always OFF there). Pass True only for a bot with the flag on,
    so its SHADOW rows' §13 signal/score/action columns are populated."""
    fields = list(_LOG_FIELDS)
    if include_cost_columns:
        fields += _COST_LOG_FIELDS
    if include_decision_columns:
        fields += _DECISION_LOG_FIELDS
    if include_shadow_columns:
        fields += _SHADOW_LOG_FIELDS
    def log(record):
        exists = _os.path.exists(path)
        with open(path, "a", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            if not exists:
                w.writeheader()
            w.writerow(record)
    return log


def runner(state, deps, now_fn, sleep_fn, poll_s, ticks, tick_fn=tick, state_path=None):
    """Call tick_fn every poll_s up to `ticks` times.
    now_fn/sleep_fn are injected so this is testable; production passes an ET clock + time.sleep.
    If state_path is given, persist state after every tick so a restart resumes (not orphans).

    A halt does NOT stop the loop: the bot must keep ticking so management/stops keep firing on
    open positions, and a transient reconcile-drift halt can self-clear once broker truth re-syncs.
    Entry is gated by state.halted inside the tick; killing the process here would orphan positions."""
    for i in range(ticks):
        state = tick_fn(state, deps, now_fn())
        if state_path:
            save_state(state, state_path)
        if i < ticks - 1:
            sleep_fn(poll_s)
    return state


from bot.broker.tradier import TradierClient, BrokerError
from bot.broker.submit import submit_and_verify, submit_entry_ladder, submit_close_ladder
from bot.broker.order_state import ExecutionResult, OrderState, map_broker_status, TERMINAL
from bot.strategy.manage import spread_value_mid, dte_from_expiry, build_close_payload, ExitAction
from bot.strategy.s2b import OptionQuote
from bot.strategy.execution_price import (package_mid, expected_executable_credit, quotes_valid,
                                          package_too_wide, spot_moved_too_far)
from bot.risk_gate import AccountState, RiskConfig
from bot.portfolio import exposure
import time
import datetime as _datetime


def build_deps(http, account_id, get_spot, get_atr, get_vix_regime,
               entry_days=frozenset({0}), max_open=1, max_entries_per_day=1, shared_account=False,
               trade_log=(lambda record: None),
               regime_log=(lambda s, ts, e: None), uw_http=None,
               poll_s=2, timeout_s=30, base_risk_pct=0.10, s2b_cfg=None, trend_gate_enabled=False,
               degross_on_risk_off=False, degross_on_flow_flip=False, features=None,
               markout_log=(lambda record: None)):
    """Assemble a production Deps from a Tradier http callable + injected market-data feeds.
    s2b_cfg overrides the spread geometry (default = standard $10-wing S2bConfig).
    features overrides the partner-review-v2 feature flags (default = all-off S2bFeatures)."""
    from bot.strategy.s2b import S2bConfig
    s2b_cfg = s2b_cfg if s2b_cfg is not None else S2bConfig()
    features = features if features is not None else S2bFeatures()
    client = TradierClient(account_id=account_id, http=http)

    # Derive the risk-gate caps from max_open so the gate can never block below the book size the
    # orchestrator targets. A book of max_open positions at base_risk_pct each needs a total-risk
    # cap of at least max_open * base_risk_pct (floored at the original 0.30).
    risk_cfg = RiskConfig(max_risk_pct=base_risk_pct,
                          max_total_risk_pct=max(0.30, round(max_open * base_risk_pct, 4)),
                          max_concurrent=max_open)

    def get_chain(symbol, expiry):
        resp = http("GET", "/markets/options/chains",
                    params={"symbol": symbol, "expiration": expiry, "greeks": "true"})
        return feeds.parse_chain(resp)

    def _quote(symbol):
        q = http("GET", "/markets/quotes", params={"symbols": symbol})["quotes"]["quote"]
        bid = q.get("bid")
        ask = q.get("ask")
        if bid is None or ask is None:
            raise ValueError(f"no market for {symbol}")
        return OptionQuote(0.0, 0.0, float(bid), float(ask))

    def _leg_quotes(pos):
        short = _quote(_occ(pos.ticker, pos.expiry, "P", pos.short_strike))
        long = _quote(_occ(pos.ticker, pos.expiry, "P", pos.long_strike))
        return short, long

    def mark_position(pos):
        short, long = _leg_quotes(pos)
        return spread_value_mid(short, long)

    def broker_legs():
        return feeds.parse_position_legs(http("GET", f"/accounts/{account_id}/positions"))

    def broker_equity():
        return feeds.parse_equity(http("GET", f"/accounts/{account_id}/balances"))

    def option_greeks_iv(symbols):
        """Priority-0 fix item 5: ONE batched Tradier greeks fetch for the BS gap-stress iv_fn.
        [OCC option symbol] -> {symbol: mid_iv}. A single /markets/quotes call (greeks=true) for the
        whole gap-stress book. Best-effort: any symbol without a usable mid_iv (falls back to smv_vol)
        is simply absent from the map, so the orchestrator's iv_fn falls back per leg. NEVER raises.
        Only ever called on the gated Bot-B gap-stress paths, so Bot C issues no such request."""
        out = {}
        if not symbols:
            return out
        try:
            resp = http("GET", "/markets/quotes",
                        params={"symbols": ",".join(symbols), "greeks": "true"})
            quotes = (resp.get("quotes") or {}).get("quote") or []
            if isinstance(quotes, dict):
                quotes = [quotes]
            for q in quotes:
                sym = q.get("symbol")
                greeks = q.get("greeks") or {}
                iv = greeks.get("mid_iv")
                if iv is None:
                    iv = greeks.get("smv_vol")
                if sym is None or iv is None:
                    continue
                try:
                    ivf = float(iv)
                except (TypeError, ValueError):
                    continue
                if ivf > 0:
                    out[sym] = ivf
        except Exception:
            pass
        return out

    def option_quotes(symbols):
        """§14 markout batching (Priority-0 fix item 4): ONE batched Tradier quote fetch for a list
        of OCC option symbols -> {symbol: mid}. mid = (bid+ask)/2 per leg. A single /markets/quotes
        call for the whole due-markout book, so run_markout_cycle prices every pending leg off the
        critical tick() path in one request instead of one per item. Best-effort: a symbol without a
        usable bid/ask is simply absent from the map (the tracker treats it as a missing mark).
        NEVER raises. Only ever called when markout_tracking is on, so Bot C issues no such request."""
        out = {}
        if not symbols:
            return out
        try:
            resp = http("GET", "/markets/quotes", params={"symbols": ",".join(symbols)})
            quotes = (resp.get("quotes") or {}).get("quote") or []
            if isinstance(quotes, dict):
                quotes = [quotes]
            for q in quotes:
                sym = q.get("symbol")
                bid = q.get("bid")
                ask = q.get("ask")
                if sym is None or bid is None or ask is None:
                    continue
                try:
                    out[sym] = (float(bid) + float(ask)) / 2.0
                except (TypeError, ValueError):
                    continue
        except Exception:
            pass
        return out

    def _to_execution_result(state, order, payload):
        """Build an ExecutionResult (spec §8) from a terminal OrderState + the raw Tradier order.
        Falls back to the requested/submitted values whenever the broker doesn't report an actual
        fill (always true against the sandbox), and synthesizes commissions when the broker
        reports none (sandbox never charges/reports fees)."""
        status = state.value
        requested_qty = int(payload.get("quantity[0]", 0) or 0)
        submitted_limit = payload.get("price")
        raw_filled_qty = order.get("exec_quantity") if isinstance(order, dict) else None
        filled_quantity = (int(raw_filled_qty) if raw_filled_qty is not None
                           else (requested_qty if status == "filled" else 0))
        raw_fill_price = order.get("avg_fill_price") if isinstance(order, dict) else None
        average_fill_price = float(raw_fill_price) if raw_fill_price is not None else submitted_limit
        raw_commission = order.get("commission") if isinstance(order, dict) else None
        raw_reg_fees = order.get("regulatory_fees") if isinstance(order, dict) else None
        if raw_commission is not None or raw_reg_fees is not None:  # broker reported at least one field
            commissions = float(raw_commission or 0.0)
            regulatory_fees = float(raw_reg_fees or 0.0)
        else:                                             # sandbox reports none -> synthetic fallback
            # one side (2 legs); the paired open+close orders sum to round_trip_commission_per_contract
            commissions = one_side_commission_per_contract(features) * filled_quantity
            regulatory_fees = 0.0
        oid = order.get("id") if isinstance(order, dict) else None
        return ExecutionResult(status=status, requested_quantity=requested_qty,
                               filled_quantity=filled_quantity, average_fill_price=average_fill_price,
                               submitted_limit=submitted_limit, commissions=commissions,
                               regulatory_fees=regulatory_fees,
                               order_id=(str(oid) if oid is not None else None))

    def _open_spread_ladder(payload):
        """Midpoint->natural entry limit ladder (partner spec §6): start at the package midpoint,
        submit ONE multileg limit order, wait, cancel+confirm, refresh quotes, step the credit
        down by entry_reprice_increment, repeat for up to entry_max_work_seconds -- never below
        the conservative expected-executable-credit floor, never holding two live opening orders.

        LIVE-VALIDATION-REQUIRED: only reachable when features.entry_price_ladder is True (never
        the case for run_s2b_live.py, whose features default to all-off). should_abort here covers
        quote validity + package width + spot-move; the credit-tier / transaction-cost / aggregate
        risk-budget / expiration-change re-checks are a live-validation follow-up -- TODO below."""
        short_sym = payload["option_symbol[0]"]
        long_sym = payload["option_symbol[1]"]
        signal_spot = get_spot("SPY")
        atr = get_atr("SPY")

        # Captures a fill discovered by cancel_fn's confirm-query racing against a broker fill --
        # the ladder core can only say "not confirmed cancelled", so surface the real fill here
        # rather than silently losing it (and rather than ever re-placing on top of it).
        late_fill = {"result": None}

        def _fresh_legs():
            return _quote(short_sym), _quote(long_sym)

        def place_fn(credit):
            rung_payload = dict(payload)
            rung_payload["price"] = round(credit, 2)
            oid = client.place_order(rung_payload)
            t0 = time.time()
            while True:
                order = client.get_order(oid)
                state = map_broker_status(order.get("status"))
                if state in TERMINAL:
                    return _to_execution_result(state, order, rung_payload)
                if time.time() - t0 >= features.entry_reprice_seconds:
                    return _to_execution_result(state, order, rung_payload)   # still open -> ladder cancels
                time.sleep(poll_s)

        def cancel_fn(order_id):
            try:
                client.cancel_order(order_id)
            except BrokerError:
                pass   # may have just filled -- confirm below rather than trust the exception
            order = client.get_order(order_id)
            state = map_broker_status(order.get("status"))
            if state == OrderState.FILLED:
                late_fill["result"] = _to_execution_result(state, order, payload)
                return False   # NOT a confirmed cancel -- tells the ladder to stop, not reprice
            return state in (OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED)

        def on_reprice(next_credit):
            short, long = _fresh_legs()
            if not quotes_valid(short.bid, short.ask, long.bid, long.ask):
                return {"abort": True}
            # TODO(live-validation, spec §6/§9): recalculate permissible qty here (risk budget,
            # settled cash) before each replace; recheck credit-tier ratio + transaction-cost gate.
            return {}

        def should_abort():
            short, long = _fresh_legs()
            if not quotes_valid(short.bid, short.ask, long.bid, long.ask):
                return True
            if package_too_wide(short.bid, short.ask, long.bid, long.ask,
                                features.max_package_width_ratio):
                return True
            if spot_moved_too_far(signal_spot, get_spot("SPY"), atr,
                                  features.max_signal_to_order_spot_move_atr):
                return True
            # TODO(live-validation, spec §6): also abort when the recalculated credit ratio falls
            # below the permitted tier, the transaction-cost gate fails, the aggregate risk budget
            # no longer permits the trade, or the expiration/chain has changed.
            return False

        start_short, start_long = _fresh_legs()
        start_credit = package_mid(start_short.bid, start_short.ask, start_long.bid, start_long.ask)
        min_credit = expected_executable_credit(start_short.bid, start_short.ask,
                                                start_long.bid, start_long.ask,
                                                features.expected_entry_slippage)

        result = submit_entry_ladder(
            place_fn=place_fn, cancel_fn=cancel_fn, start_credit=start_credit,
            min_credit=min_credit, step=features.entry_reprice_increment,
            max_seconds=features.entry_max_work_seconds, now_fn=time.time, sleep_fn=time.sleep,
            on_reprice=on_reprice, should_abort=should_abort)

        return late_fill["result"] if late_fill["result"] is not None else result

    def open_spread(payload):
        if features.entry_price_ladder:
            return _open_spread_ladder(payload)
        state, order = submit_and_verify(client, payload, poll_s, timeout_s, time.time, time.sleep)
        return _to_execution_result(state, order, payload)

    def _close_spread_ladder(pos, start_short, start_long, natural):
        """Package-mid->natural DEBIT close ladder (partner spec §7 take-profit exits): start at
        the package midpoint (short_mid - long_mid, the cheapest close), submit ONE multileg debit
        order, wait, cancel+confirm, refresh quotes, step the debit UP by entry_reprice_increment
        toward (never past) the natural, repeat for up to entry_max_work_seconds -- never holding
        two live closing orders. "Up" here means paying MORE to get filled, the mirror image of
        the entry credit ladder stepping down.

        LIVE-VALIDATION-REQUIRED: only reachable when features.tp_price_ladder is True AND action
        is TAKE_PROFIT (never the case for run_s2b_live.py, whose features default to all-off).
        STOP/TIME_EXIT/DEGROSS/FLOW_DEGROSS always use the single marketable-natural submit
        regardless of this flag -- spec §7 requires stops to never delay for price improvement."""
        last_payload = {"payload": None}
        late_fill = {"result": None}

        def _fresh_legs():
            return _leg_quotes(pos)

        def place_fn(debit):
            payload = build_close_payload(pos, limit_price=debit)
            last_payload["payload"] = payload
            oid = client.place_order(payload)
            t0 = time.time()
            while True:
                order = client.get_order(oid)
                state = map_broker_status(order.get("status"))
                if state in TERMINAL:
                    return _to_execution_result(state, order, payload)
                if time.time() - t0 >= features.entry_reprice_seconds:
                    return _to_execution_result(state, order, payload)   # still open -> ladder cancels
                time.sleep(poll_s)

        def cancel_fn(order_id):
            try:
                client.cancel_order(order_id)
            except BrokerError:
                pass   # may have just filled -- confirm below rather than trust the exception
            order = client.get_order(order_id)
            state = map_broker_status(order.get("status"))
            if state == OrderState.FILLED:
                late_fill["result"] = _to_execution_result(state, order, last_payload["payload"])
                return False   # NOT a confirmed cancel -- tells the ladder to stop, not reprice
            return state in (OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED)

        def on_reprice(next_debit):
            short, long = _fresh_legs()   # refresh quotes before every replacement (spec §7)
            if not quotes_valid(short.bid, short.ask, long.bid, long.ask):
                return {"abort": True}
            # TODO(live-validation, spec §7): re-derive the natural from the refreshed quotes and
            # escalate/clamp if the market has moved past the originally-captured natural.
            return {}

        def should_abort():
            short, long = _fresh_legs()
            return not quotes_valid(short.bid, short.ask, long.bid, long.ask)

        start_debit = package_mid(start_short.bid, start_short.ask, start_long.bid, start_long.ask)

        result = submit_close_ladder(
            place_fn=place_fn, cancel_fn=cancel_fn, start_debit=start_debit,
            max_debit=natural, step=features.entry_reprice_increment,
            max_seconds=features.entry_max_work_seconds, now_fn=time.time, sleep_fn=time.sleep,
            on_reprice=on_reprice, should_abort=should_abort)

        return late_fill["result"] if late_fill["result"] is not None else result

    def close_spread(pos, action):
        short, long = _leg_quotes(pos)
        limit = round(short.ask - long.bid, 2)   # marketable cost-to-close -> fills under stress
        # take-profit ladder (spec §7) is opt-in and TAKE_PROFIT-only: stops/time-exits/degrosses
        # must never delay for price improvement, so they always take the single marketable-natural
        # path below -- this branch is the ONLY behavior change, and only when tp_price_ladder is on.
        if features.tp_price_ladder and action == ExitAction.TAKE_PROFIT:
            return _close_spread_ladder(pos, short, long, limit)
        payload = build_close_payload(pos, limit_price=limit)
        state, order = submit_and_verify(client, payload, poll_s, timeout_s, time.time, time.sleep)
        return _to_execution_result(state, order, payload)

    def account_state(today, concurrent):
        eq = broker_equity()
        return AccountState(eq, eq, 0.0, concurrent, 0.0, {}, today)

    def pick_expiry(today):
        # broker-aware: pick from the actual listed expirations so market holidays are handled
        resp = http("GET", "/markets/options/expirations", params={"symbol": "SPY"})
        return feeds.pick_expiry_from_list(feeds.parse_expirations(resp), today)

    def get_expirations(today):
        """T8 (spec §14): the broker's listed SPY expirations, for alternate-expiration evaluation.
        Best-effort -- returns [] on any failure, which makes the orchestrator fall back to
        pick_expiry's single choice rather than failing the cycle."""
        try:
            resp = http("GET", "/markets/options/expirations", params={"symbol": "SPY"})
            return feeds.parse_expirations(resp)
        except Exception:
            return []

    from bot.regime.engine import compute_regime_state
    from bot.regime.vix_term import fetch_vix_term
    from bot.regime.flow_uw import flow_context

    _peak = {"v": 0.0}
    def regime_provider():
        try:
            eq = broker_equity()
            _peak["v"] = max(_peak["v"], eq or 0.0)
            today = _datetime.date.today().strftime("%Y-%m-%d")
            start = (_datetime.date.today()       # ~300 calendar days -> ~200 trading days for the 200d MA gate
                     - _datetime.timedelta(days=320)).strftime("%Y-%m-%d")
            hist = http("GET", "/markets/history",
                        params={"symbol": "SPY", "interval": "daily", "start": start, "end": today})
            bars = feeds.parse_history(hist)
            flow = flow_context(uw_http) if uw_http is not None else ("neutral", False)
            return compute_regime_state(
                vix=fetch_vix_term(), bars=bars,
                positions=feeds.reconstruct_spreads(broker_legs()),
                equity=eq, equity_peak=_peak["v"], flow=flow)
        except Exception:
            from bot.regime.state import RegimeState
            return RegimeState()

    # ── §13 shadow monitor data fetch (partner review v2 §13) ────────────────────────────────────
    # Only ever CALLED when deps.features.regime_shadow_monitor is on (gated in the orchestrator's
    # run_shadow_monitor_cycle) -- LOG ONLY, never feeds back into any order/size/entry decision.

    _SHADOW_BASKET = ("SPY", "QQQ", "DIA", "SOXX", "VIX", "VIX1D")

    def _fetch_basket_quotes():
        """One bulk Tradier quote fetch for SPY + the cross-asset/vol basket. Returns
        {symbol: {"last":.., "high":.., "low":.., "change_percentage":..}}, defaulting every
        field to None per-symbol on any parse hiccup. NEVER raises."""
        out = {s: {"last": None, "high": None, "low": None, "change_percentage": None}
              for s in _SHADOW_BASKET}
        try:
            resp = http("GET", "/markets/quotes", params={"symbols": ",".join(_SHADOW_BASKET)})
            quotes = (resp.get("quotes") or {}).get("quote") or []
            if isinstance(quotes, dict):
                quotes = [quotes]
            for q in quotes:
                sym = q.get("symbol")
                if sym not in out:
                    continue
                for field in ("last", "high", "low", "change_percentage"):
                    v = q.get(field)
                    out[sym][field] = float(v) if v is not None else None
        except Exception:
            pass
        return out

    def _session_vwap_and_opening_range(symbol, today_str):
        """Best-effort intraday VWAP (volume-weighted typical price over the session so far) +
        opening-range high/low (first ~30 one-minute bars) from Tradier timesales.
        (vwap, or_high, or_low); any/all may be None. NEVER raises."""
        try:
            resp = http("GET", "/markets/timesales",
                       params={"symbol": symbol, "interval": "1min",
                               "start": f"{today_str} 09:30", "end": f"{today_str} 16:00",
                               "session_filter": "open"})
            series = (resp.get("series") or {}).get("data") or []
            if isinstance(series, dict):
                series = [series]
            if not series:
                return None, None, None
            or_bars = series[:30]
            or_high = max(float(b["high"]) for b in or_bars) if or_bars else None
            or_low = min(float(b["low"]) for b in or_bars) if or_bars else None
            pv_sum, vol_sum = 0.0, 0.0
            for b in series:
                h, l, c = float(b["high"]), float(b["low"]), float(b["close"])
                v = float(b.get("volume") or 0)
                pv_sum += ((h + l + c) / 3.0) * v
                vol_sum += v
            vwap = round(pv_sum / vol_sum, 4) if vol_sum > 0 else None
            return vwap, or_high, or_low
        except Exception:
            return None, None, None

    def _fetch_put_greeks(symbol, expiry):
        """Raw Tradier chain fetch for PUTS carrying gamma/IV (feeds.parse_chain only keeps
        delta/bid/ask). NEVER raises; [] on any failure."""
        try:
            resp = http("GET", "/markets/options/chains",
                       params={"symbol": symbol, "expiration": expiry, "greeks": "true"})
            options = (resp.get("options") or {}).get("option") or []
            if isinstance(options, dict):
                options = [options]
            out = []
            for o in options:
                if o.get("option_type") != "put":
                    continue
                greeks = o.get("greeks") or {}
                delta = greeks.get("delta")
                if delta is None or o.get("bid") is None or o.get("ask") is None:
                    continue
                iv = greeks.get("mid_iv")
                if iv is None:
                    iv = greeks.get("smv_vol")
                out.append({"strike": float(o["strike"]), "delta": abs(float(delta)),
                           "gamma": greeks.get("gamma"), "iv": (float(iv) if iv is not None else None),
                           "bid": float(o["bid"]), "ask": float(o["ask"])})
            return out
        except Exception:
            return []

    def _select_shadow_short(puts, spot, atr, cfg):
        """Mirrors bot.strategy.s2b.select_short_put's delta-band + ATR-cushion selection, on the
        raw {strike, delta, gamma, iv} dicts above (which carry gamma/iv that OptionQuote doesn't).
        None if nothing qualifies (missing spot/atr, zero atr, no eligible strike)."""
        if not puts or not atr or spot is None:
            return None
        eligible = [o for o in puts
                   if o["strike"] < spot and (spot - o["strike"]) / atr >= cfg.min_cushion_atr
                   and cfg.min_delta <= o["delta"] <= cfg.max_delta]
        if not eligible:
            return None
        return min(eligible, key=lambda o: abs(o["delta"] - cfg.target_delta))

    def _put_skew_proxy(puts, short):
        """Simple skew proxy: IV of the most-OTM available put (lowest strike) minus the shadow
        short leg's IV. Positive = downside puts pricier (fear); None if not computable."""
        if not puts or short is None or short.get("iv") is None:
            return None
        far = [o for o in puts if o["strike"] < short["strike"] and o.get("iv") is not None]
        if not far:
            return None
        farthest = min(far, key=lambda o: o["strike"])
        return round(farthest["iv"] - short["iv"], 4)

    def shadow_data():
        """§13 raw signal fetch (LOG ONLY -- see bot/regime/shadow_monitor.py for the scoring).
        Best-effort: any leg that can't be fetched/parsed is None; the whole thing NEVER raises.
        breadth/up_down_vol/whale_flow stay None (not fetchable today -- UW is down)."""
        from zoneinfo import ZoneInfo
        today = _datetime.datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        out = {k: None for k in (
            "spy", "spy_vwap", "spy_atr", "session_high", "session_low", "opening_range",
            "qqq_ret", "dia_ret", "soxx_ret", "spy_ret", "qqq_vs_vwap", "soxx_vs_vwap",
            "vix", "vix1d", "put_skew", "short_delta", "short_gamma", "short_iv",
            "breadth", "up_down_vol", "whale_flow")}
        try:
            out["spy"] = get_spot("SPY")
        except Exception:
            pass
        try:
            out["spy_atr"] = get_atr("SPY")
        except Exception:
            pass

        quotes = _fetch_basket_quotes()
        spy_q = quotes["SPY"]
        out["session_high"] = spy_q["high"]
        out["session_low"] = spy_q["low"]
        out["spy_ret"] = ((spy_q["change_percentage"] / 100.0)
                         if spy_q["change_percentage"] is not None else None)
        for tag, sym in (("qqq_ret", "QQQ"), ("dia_ret", "DIA"), ("soxx_ret", "SOXX")):
            cp = quotes[sym]["change_percentage"]
            out[tag] = (cp / 100.0) if cp is not None else None
        out["vix"] = quotes["VIX"]["last"]
        out["vix1d"] = quotes["VIX1D"]["last"]

        vwap, or_high, or_low = _session_vwap_and_opening_range("SPY", today)
        out["spy_vwap"] = vwap
        out["opening_range"] = {"high": or_high, "low": or_low}

        qqq_vwap, _, _ = _session_vwap_and_opening_range("QQQ", today)
        soxx_vwap, _, _ = _session_vwap_and_opening_range("SOXX", today)
        qqq_last, soxx_last = quotes["QQQ"]["last"], quotes["SOXX"]["last"]
        out["qqq_vs_vwap"] = ((qqq_last - qqq_vwap)
                              if qqq_last is not None and qqq_vwap is not None else None)
        out["soxx_vs_vwap"] = ((soxx_last - soxx_vwap)
                               if soxx_last is not None and soxx_vwap is not None else None)

        try:
            expiry = pick_expiry(today)
            puts = _fetch_put_greeks("SPY", expiry)
            short = _select_shadow_short(puts, out["spy"], out["spy_atr"], s2b_cfg)
            if short is not None:
                out["short_delta"] = short["delta"]
                out["short_gamma"] = short.get("gamma")
                out["short_iv"] = short.get("iv")
                out["put_skew"] = _put_skew_proxy(puts, short)
        except Exception:
            pass
        return out

    return Deps(
        get_spot=get_spot, get_atr=get_atr, get_chain=get_chain,
        pick_expiry=pick_expiry,
        get_expirations=get_expirations,
        get_vix_regime=get_vix_regime, account_state=account_state,
        mark_position=mark_position, dte_of=lambda p, today: dte_from_expiry(p.expiry, today),
        open_spread=open_spread, close_spread=close_spread,
        broker_positions=lambda: feeds.reconstruct_spreads(broker_legs()),
        broker_equity=broker_equity, bot_equity=broker_equity,
        alert_sink=lambda alerts: [print(f"[ALERT] {a.severity.value}: {a.message}") for a in alerts],
        shadow_data=shadow_data,
        markout_log=markout_log,
        option_quotes=option_quotes,
        base_risk_pct=base_risk_pct, risk_cfg=risk_cfg, s2b_cfg=s2b_cfg,
        entry_days=entry_days, max_open=max_open, max_entries_per_day=max_entries_per_day,
        shared_account=shared_account, trade_log=trade_log,
        regime_provider=regime_provider, regime_log=regime_log,
        trend_gate_enabled=trend_gate_enabled,
        degross_on_risk_off=degross_on_risk_off,
        degross_on_flow_flip=degross_on_flow_flip,
        features=features,
        risk_equity=lambda: exposure.risk_equity(features.allocated_equity, broker_equity()),
        account_spy_exposure=lambda: exposure.account_spy_exposure(feeds.reconstruct_spreads(broker_legs())),
        account_spy_spreads=lambda: feeds.reconstruct_spreads(broker_legs()),
        option_greeks_iv=option_greeks_iv,
    )
