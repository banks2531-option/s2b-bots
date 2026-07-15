"""Production wiring: build Deps from Tradier and the runner (spec §6,§7,§8)."""
import csv as _csv
import os as _os

from bot.app import feeds
from bot.app.orchestrator import Deps, tick
from bot.app.state_store import save_state
from bot.strategy.s2b import _occ
from bot.features import S2bFeatures

_LOG_FIELDS = ["event", "date", "ticker", "short", "long", "expiry", "qty",
               "credit", "action", "exit_value", "pnl", "status"]
_COST_LOG_FIELDS = ["gross_pnl", "net_pnl"]   # only added when actual_fill_accounting is on (spec §8)


def make_trade_logger(path, include_cost_columns=False):
    """Return a callable that appends a trade-record dict to a CSV (header written once).
    Used per-bot so a shared-account A/B can be measured from separate files.

    include_cost_columns=False (default) keeps the original 11-column CSV shape byte-identical --
    critical for the real-money live bot (run_s2b_live.py), whose actual_fill_accounting is always
    OFF. Pass True only for a bot with the flag on, so its gross_pnl/net_pnl columns are populated."""
    fields = _LOG_FIELDS + _COST_LOG_FIELDS if include_cost_columns else _LOG_FIELDS
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


from bot.broker.tradier import TradierClient
from bot.broker.submit import submit_and_verify
from bot.broker.order_state import ExecutionResult
from bot.strategy.manage import spread_value_mid, dte_from_expiry, build_close_payload
from bot.strategy.s2b import OptionQuote
from bot.risk_gate import AccountState, RiskConfig
from bot.portfolio import exposure
import time
import datetime as _datetime


def build_deps(http, account_id, get_spot, get_atr, get_vix_regime,
               entry_days=frozenset({0}), max_open=1, max_entries_per_day=1, shared_account=False,
               trade_log=(lambda record: None),
               regime_log=(lambda s, ts, e: None), uw_http=None,
               poll_s=2, timeout_s=30, base_risk_pct=0.10, s2b_cfg=None, trend_gate_enabled=False,
               degross_on_risk_off=False, degross_on_flow_flip=False, features=None):
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
            commissions = features.est_commission_per_leg_rt * 2 * filled_quantity   # 2 legs
            regulatory_fees = 0.0
        oid = order.get("id") if isinstance(order, dict) else None
        return ExecutionResult(status=status, requested_quantity=requested_qty,
                               filled_quantity=filled_quantity, average_fill_price=average_fill_price,
                               submitted_limit=submitted_limit, commissions=commissions,
                               regulatory_fees=regulatory_fees,
                               order_id=(str(oid) if oid is not None else None))

    def open_spread(payload):
        state, order = submit_and_verify(client, payload, poll_s, timeout_s, time.time, time.sleep)
        return _to_execution_result(state, order, payload)

    def close_spread(pos, action):
        short, long = _leg_quotes(pos)
        limit = round(short.ask - long.bid, 2)   # marketable cost-to-close -> fills under stress
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

    return Deps(
        get_spot=get_spot, get_atr=get_atr, get_chain=get_chain,
        pick_expiry=pick_expiry,
        get_vix_regime=get_vix_regime, account_state=account_state,
        mark_position=mark_position, dte_of=lambda p, today: dte_from_expiry(p.expiry, today),
        open_spread=open_spread, close_spread=close_spread,
        broker_positions=lambda: feeds.reconstruct_spreads(broker_legs()),
        broker_equity=broker_equity, bot_equity=broker_equity,
        alert_sink=lambda alerts: [print(f"[ALERT] {a.severity.value}: {a.message}") for a in alerts],
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
    )
