from bot.app.wiring import runner
from bot.app.orchestrator import BotState, tick


def test_runner_calls_tick_n_times_then_stops():
    calls = {"n": 0}

    def fake_tick(state, deps, now):
        calls["n"] += 1
        return state

    clock = {"t": 0}
    def now_fn(): return clock["t"]
    def sleep_fn(s): clock["t"] += s

    runner(BotState(), deps=None, now_fn=now_fn, sleep_fn=sleep_fn,
           poll_s=60, ticks=3, tick_fn=fake_tick)
    assert calls["n"] == 3


def test_runner_keeps_running_when_halted():
    # A halt must NOT kill the process: the bot has to keep ticking so management/stops keep
    # firing on open positions (and a transient reconcile-drift halt can self-clear). Entries
    # are gated inside tick by state.halted, not by stopping the runner.
    calls = {"n": 0}
    def halting_tick(state, deps, now):
        calls["n"] += 1
        state.halted = True
        return state

    runner(BotState(), deps=None, now_fn=lambda: 0, sleep_fn=lambda s: None,
           poll_s=60, ticks=10, tick_fn=halting_tick)
    assert calls["n"] == 10   # stays alive and keeps managing despite the halt


from bot.app.wiring import build_deps
from datetime import datetime


def _fake_http(responses):
    def http(method, path, params=None, data=None):
        for key, resp in responses.items():
            if key in path:
                return resp
        if "/orders" in path:
            return {"order": {"id": 1, "status": "filled"}}
        return {}
    return http


def test_build_deps_drives_a_clean_monday_entry_tick():
    chain = {"options": {"option": [
        {"strike": 568.0, "option_type": "put", "bid": 3.40, "ask": 3.50, "greeks": {"delta": -0.36}},
        {"strike": 558.0, "option_type": "put", "bid": 1.60, "ask": 1.70, "greeks": {"delta": -0.18}},
        {"strike": 560.0, "option_type": "put", "bid": 2.00, "ask": 2.10, "greeks": {"delta": -0.22}},
        {"strike": 565.0, "option_type": "put", "bid": 2.80, "ask": 2.90, "greeks": {"delta": -0.30}},
    ]}}
    http = _fake_http({
        "/markets/options/expirations": {"expirations": {"date": ["2026-06-19", "2026-06-26"]}},
        "/markets/options/chains": chain,
        "/balances": {"balances": {"total_equity": 20_000.0}},
        "/positions": {"positions": "null"},
        "/markets/quotes": {"quotes": {"quote": {"symbol": "SPY", "bid": 574.9, "ask": 575.1, "last": 575.0}}},
    })
    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01))
    state = tick(BotState(), deps, datetime(2026, 6, 15, 10, 5))   # Monday 10:05
    assert len(state.open_positions) == 1 and state.open_positions[0].short_strike == 568.0


def test_option_greeks_iv_batched_maps_mid_iv_with_smv_vol_fallback():
    # Priority-0 fix item 5: the wired batched greeks fetch parses mid_iv (falling back to smv_vol)
    # for a multi-symbol /markets/quotes call, in ONE request, dropping symbols without a usable IV.
    calls = []
    def http(method, path, params=None, data=None):
        calls.append((path, params))
        return {"quotes": {"quote": [
            {"symbol": "SPY260619P00568000", "greeks": {"mid_iv": 0.21}},
            {"symbol": "SPY260619P00558000", "greeks": {"smv_vol": 0.24}},   # mid_iv missing -> smv_vol
            {"symbol": "SPY260619P00560000", "greeks": {"mid_iv": 0.0}},      # non-positive -> dropped
            {"symbol": "SPY260619P00565000", "greeks": {}},                    # no IV -> dropped
        ]}}
    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01))
    syms = ["SPY260619P00568000", "SPY260619P00558000", "SPY260619P00560000", "SPY260619P00565000"]
    got = deps.option_greeks_iv(syms)
    assert got == {"SPY260619P00568000": 0.21, "SPY260619P00558000": 0.24}
    quote_calls = [c for c in calls if c[0] == "/markets/quotes"]
    assert len(quote_calls) == 1                                   # single batched request
    assert quote_calls[0][1]["symbols"] == ",".join(syms)          # all symbols in one call
    assert quote_calls[0][1]["greeks"] == "true"


def test_build_deps_broker_positions_reconstructs():
    positions_resp = {"positions": {"position": [
        {"symbol": "SPY260619P00568000", "quantity": -2},
        {"symbol": "SPY260619P00558000", "quantity": 2},
    ]}}
    http = _fake_http({
        "/positions": positions_resp,
        "/balances": {"balances": {"total_equity": 20_000.0}},
        "/markets/quotes": {"quotes": {"quote": {"bid": 3.40, "ask": 3.50}}},
    })
    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01))
    result = deps.broker_positions()
    assert len(result) == 1
    assert result[0].short_strike == 568.0


def test_build_deps_risk_gate_caps_track_max_open():
    # The risk gate's concurrency + total-risk caps must be derived from max_open so they can't
    # drift out of sync with the orchestrator (which would let the bot think it can hold N while
    # the gate blocks at a lower number). 5 positions at 10%/trade -> total-risk cap >= 50%.
    http = _fake_http({"/balances": {"balances": {"total_equity": 20_000.0}}})
    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01), max_open=5, base_risk_pct=0.10)
    assert deps.max_open == 5
    assert deps.risk_cfg.max_concurrent == 5
    assert deps.risk_cfg.max_total_risk_pct >= 0.50 - 1e-9


from bot.app.wiring import make_trade_logger
import csv as _csv


def test_make_trade_logger_writes_csv(tmp_path):
    p = tmp_path / "trades_monday.csv"
    log = make_trade_logger(str(p))
    log({"event": "OPEN", "date": "2026-06-15", "ticker": "SPY", "short": 568.0,
         "long": 558.0, "qty": 2, "credit": 1.7, "status": "filled"})
    log({"event": "CLOSE", "date": "2026-06-19", "ticker": "SPY", "action": "take_profit",
         "pnl": 85.0, "status": "filled"})
    rows = list(_csv.DictReader(open(str(p))))
    assert len(rows) == 2
    assert rows[0]["event"] == "OPEN" and rows[0]["credit"] == "1.7"
    assert rows[1]["event"] == "CLOSE" and rows[1]["pnl"] == "85.0"


def test_build_deps_threads_narrow_wing_and_small_account_risk():
    # the small-account live variant needs a narrow wing + a higher per-trade % (one narrow spread
    # is already a large fraction of a tiny account). Both must flow through build_deps.
    from bot.strategy.s2b import S2bConfig
    http = _fake_http({"/balances": {"balances": {"total_equity": 402.0}}})
    deps = build_deps(http, account_id="6YB71948", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01), max_open=1,
                      s2b_cfg=S2bConfig(wing_width=2), base_risk_pct=0.45)
    assert deps.s2b_cfg.wing_width == 2
    assert deps.risk_cfg.max_risk_pct == 0.45
    assert deps.risk_cfg.max_concurrent == 1


# ── Fix 2: the live bot's trade-log CSV must stay byte-identical (11 columns, no gross/net) ──────

def test_make_trade_logger_default_csv_has_no_cost_columns(tmp_path):
    p = tmp_path / "trades_live.csv"
    log = make_trade_logger(str(p))   # default -> flag-off shape
    log({"event": "OPEN", "date": "2026-06-15", "ticker": "SPY", "short": 568.0,
         "long": 558.0, "qty": 2, "credit": 1.7, "status": "filled",
         "gross_pnl": 999.0, "net_pnl": 888.0})   # extra keys must be ignored/dropped
    header = open(str(p)).readline().strip().split(",")
    assert header == ["event", "date", "ticker", "short", "long", "expiry", "qty",
                      "credit", "action", "exit_value", "pnl", "status"]
    assert "gross_pnl" not in header and "net_pnl" not in header


def test_partial_open_row_status_survives_12col_csv(tmp_path):
    # Fix A regression guard at the REAL CSV boundary: a partial OPEN row marks itself via the
    # `status` cell = "partial_fill" (NOT an extra "partial" column). Written through the actual
    # DictWriter(extrasaction="ignore"), the header stays exactly the 12 _LOG_FIELDS AND the row's
    # status cell reads "partial_fill" -- i.e. the marker survives, unlike a dropped extra key.
    from bot.app.wiring import _LOG_FIELDS
    p = tmp_path / "trades_live.csv"
    log = make_trade_logger(str(p))   # default -> flag-off (Bot C) 12-column shape
    # an OPEN row exactly as _record_open emits it for a partial fill (self-describing status;
    # a stray "partial" key here would be silently dropped by extrasaction="ignore").
    log({"event": "OPEN", "date": "2026-06-15", "ticker": "SPY", "short": 568.0, "long": 558.0,
         "expiry": "2026-06-19", "qty": 1, "credit": 1.7, "status": "partial_fill", "partial": True})
    rows = list(_csv.DictReader(open(str(p))))
    header = open(str(p)).readline().strip().split(",")
    assert header == _LOG_FIELDS                       # exactly 12 columns, no "partial" column
    assert len(_LOG_FIELDS) == 12
    assert rows[0]["status"] == "partial_fill"         # the marker reached the CSV via the status cell
    assert "partial" not in rows[0]                    # the extra key was dropped, as expected


def test_make_trade_logger_include_cost_columns_true_adds_gross_and_net(tmp_path):
    p = tmp_path / "trades_alldays.csv"
    log = make_trade_logger(str(p), include_cost_columns=True)
    log({"event": "CLOSE", "date": "2026-06-19", "ticker": "SPY", "action": "take_profit",
         "pnl": 85.0, "gross_pnl": 100.0, "net_pnl": 85.0, "status": "filled"})
    header = open(str(p)).readline().strip().split(",")
    assert header == ["event", "date", "ticker", "short", "long", "expiry", "qty",
                      "credit", "action", "exit_value", "pnl", "status",
                      "gross_pnl", "net_pnl"]


def test_build_and_run_gates_include_cost_columns_on_actual_fill_accounting_flag():
    # the live bot (run_s2b_live.py) never passes features= -> S2bFeatures() default -> flag off ->
    # build_and_run must call make_trade_logger with include_cost_columns=False (byte-identical CSV).
    import inspect
    from bot.app import run_s2b
    src = inspect.getsource(run_s2b.build_and_run)
    assert "include_cost_columns=resolved_features.actual_fill_accounting" in src


def test_decision_row_with_new_risk_sizing_fields_survives_logger(tmp_path):
    # spec §11: a DECISION record carrying the new risk-sizing telemetry (limiting_gate, final_qty,
    # decision_outcome, requested_qty, quality_adjusted_qty, risk_* exposure fields) must NOT be
    # dropped by the DictWriter's extrasaction="ignore" -- every emitted key must be in the header.
    p = tmp_path / "trades_alldays.csv"
    log = make_trade_logger(str(p), include_decision_columns=True)
    log({"event": "DECISION", "decision": "blocked", "limiting_gate": "gap_1_5atr",
         "requested_qty": 2, "quality_adjusted_qty": 1, "final_qty": 0,
         "risk_current_exposure": 4210.0, "risk_limit": 4320.0,
         "risk_remaining_capacity": 110.0, "risk_incremental_per_contract": 285.0,
         "decision_outcome": "blocked_zero_capacity"})
    header = open(str(p)).readline().strip().split(",")
    for col in ("limiting_gate", "requested_qty", "quality_adjusted_qty", "final_qty",
                "risk_current_exposure", "risk_limit", "risk_remaining_capacity",
                "risk_incremental_per_contract", "decision_outcome"):
        assert col in header
    rows = list(_csv.DictReader(open(str(p))))
    assert rows[0]["limiting_gate"] == "gap_1_5atr"
    assert rows[0]["final_qty"] == "0"
    assert rows[0]["decision_outcome"] == "blocked_zero_capacity"


# ── Fix 3: a genuinely-reported 0.0 commission/fee must NOT trigger the synthetic fallback ───────

def test_reported_zero_commission_and_fees_are_not_treated_as_unreported():
    # the broker explicitly reported commission=0.0 and regulatory_fees=0.0 (e.g. a fee-free promo
    # or an already-net fill) -> must be used as-is, NOT overridden by the synthetic estimate.
    from bot.features import S2bFeatures

    def http(method, path, params=None, data=None):
        if "/balances" in path:
            return {"balances": {"total_equity": 20_000.0}}
        if "/orders" in path and method == "POST":
            return {"order": {"id": 7, "status": "ok"}}
        if "/orders/7" in path:
            return {"order": {"id": 7, "status": "filled", "avg_fill_price": 1.65,
                              "exec_quantity": 2, "commission": 0.0, "regulatory_fees": 0.0}}
        return {}

    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01),
                      features=S2bFeatures(commission_per_contract_per_leg_per_side=0.70))
    result = deps.open_spread({"price": 1.70, "quantity[0]": 2})
    assert result.commissions == 0.0        # NOT the synthetic 0.70*2*2 = 2.80
    assert result.regulatory_fees == 0.0


def test_reported_zero_commission_only_still_uses_reported_fees_field():
    # broker reports commission=0.0 but omits regulatory_fees entirely -> commission is honored as
    # reported (0.0), regulatory_fees falls back to 0.0 too (not the synthetic commission estimate).
    from bot.features import S2bFeatures

    def http(method, path, params=None, data=None):
        if "/balances" in path:
            return {"balances": {"total_equity": 20_000.0}}
        if "/orders" in path and method == "POST":
            return {"order": {"id": 8, "status": "ok"}}
        if "/orders/8" in path:
            return {"order": {"id": 8, "status": "filled", "avg_fill_price": 1.65,
                              "exec_quantity": 2, "commission": 0.0}}
        return {}

    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01),
                      features=S2bFeatures(commission_per_contract_per_leg_per_side=0.70))
    result = deps.open_spread({"price": 1.70, "quantity[0]": 2})
    assert result.commissions == 0.0     # reported field honored, no synthetic fallback triggered
    assert result.regulatory_fees == 0.0


def test_build_deps_attaches_regime_provider_that_returns_state():
    http = _fake_http({
        "/balances": {"balances": {"total_equity": 20000.0}},
        "/positions": {"positions": "null"},
        "/markets/history": {"history": {"day": [
            {"date": "2026-06-01", "high": 101, "low": 99, "close": 100}] * 30}},
    })
    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01),
                      regime_log=lambda s, ts, e: None)
    assert deps.regime_provider is not None
    rs = deps.regime_provider()           # must return a RegimeState, never raise
    from bot.regime.state import RegimeState
    assert isinstance(rs, RegimeState)


def test_build_deps_risk_equity_caps_at_allocation(): # partner review v2 §2
    http = _fake_http({"/balances": {"balances": {"total_equity": 80_000.0}}})
    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01))
    assert deps.risk_equity() == 72_000.0   # default allocated_equity from S2bFeatures


def test_build_deps_risk_equity_uses_broker_equity_when_below_allocation():
    http = _fake_http({"/balances": {"balances": {"total_equity": 60_000.0}}})
    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01))
    assert deps.risk_equity() == 60_000.0


def test_build_deps_account_spy_exposure_includes_foreign_positions():
    # A position not opened by this bot (reconstructed with credit=0.0) must still count toward
    # account-level SPY exposure per partner spec §2 ("must not ignore them when calculating
    # total SPY risk").
    positions_resp = {"positions": {"position": [
        {"symbol": "SPY260619P00568000", "quantity": -2},
        {"symbol": "SPY260619P00558000", "quantity": 2},
    ]}}
    http = _fake_http({
        "/positions": positions_resp,
        "/balances": {"balances": {"total_equity": 20_000.0}},
    })
    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01))
    exposure = deps.account_spy_exposure()
    # width 10, credit 0.0 (unknown from broker) -> structural = 10*100*2 = 2000, stop = same (conservative)
    assert exposure == {"structural": 2000.0, "stop": 2000.0}


# ── Task 3.1: entry_price_ladder wired into open_spread (partner spec §6) ────────────────────────

def _spread_payload(price=1.70, qty=2):
    return {
        "class": "multileg", "symbol": "SPY", "type": "credit", "duration": "day",
        "price": price,
        "option_symbol[0]": "SPY260619P00568000", "side[0]": "sell_to_open", "quantity[0]": qty,
        "option_symbol[1]": "SPY260619P00558000", "side[1]": "buy_to_open", "quantity[1]": qty,
    }


def _ladder_http(order_scripts, short_quote=(3.40, 3.50), long_quote=(1.60, 1.70)):
    """order_scripts: list of status-lists, one per order placed (in placement order); the last
    status in a list repeats once its cursor runs off the end. A DELETE forces the next GET on
    that order to report 'canceled', modelling a confirmed cancel."""
    scripts = list(order_scripts)
    state = {"next_id": 500, "orders": {}}
    posts = []
    deletes = []

    def http(method, path, params=None, data=None):
        if path == "/markets/quotes":
            sym = params["symbols"]
            bid, ask = (short_quote if sym == "SPY260619P00568000" else long_quote)
            return {"quotes": {"quote": {"symbol": sym, "bid": bid, "ask": ask}}}
        if path.endswith("/orders") and method == "POST":
            posts.append(dict(data))
            oid = str(state["next_id"]); state["next_id"] += 1
            script = scripts.pop(0) if scripts else ["filled"]
            state["orders"][oid] = {"script": script, "cursor": 0, "cancelled": False}
            return {"order": {"id": oid}}
        if "/orders/" in path and method == "GET":
            oid = path.rsplit("/", 1)[-1]
            rec = state["orders"][oid]
            if rec["cancelled"]:
                return {"order": {"id": oid, "status": "canceled"}}
            i = min(rec["cursor"], len(rec["script"]) - 1)
            status = rec["script"][i]
            rec["cursor"] += 1
            body = {"id": oid, "status": status}
            if status == "filled":
                body["exec_quantity"] = 2
                body["avg_fill_price"] = 1.75
            return {"order": body}
        if "/orders/" in path and method == "DELETE":
            oid = path.rsplit("/", 1)[-1]
            state["orders"][oid]["cancelled"] = True
            deletes.append(oid)
            return {}
        return {}
    return http, posts, deletes


def test_open_spread_ladder_off_is_exact_single_submit_behavior():
    # default S2bFeatures() -> entry_price_ladder=False (always the case for run_s2b_live.py) --
    # open_spread must be the pre-existing single-submit call: ONE POST at the payload's own
    # price (never recomputed to package_mid), no cancel/reprice machinery touched at all.
    http, posts, deletes = _ladder_http([["filled"]])
    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01))
    assert deps.features.entry_price_ladder is False
    result = deps.open_spread(_spread_payload(price=1.70, qty=2))
    assert result.status == "filled"
    assert len(posts) == 1
    assert posts[0]["price"] == 1.70          # untouched -- not recomputed to package_mid (1.80)
    assert deletes == []


def test_open_spread_ladder_on_starts_at_package_mid_and_fills_first_rung():
    from bot.features import S2bFeatures
    # short 3.40/3.50 (mid 3.45), long 1.60/1.70 (mid 1.65) -> package_mid = 1.80, natural = 1.70
    http, posts, deletes = _ladder_http([["filled"]])
    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01), poll_s=0.01,
                      features=S2bFeatures(entry_price_ladder=True, entry_reprice_seconds=0.02,
                                          entry_max_work_seconds=5.0))
    result = deps.open_spread(_spread_payload(price=1.70, qty=2))   # payload's price is irrelevant now
    assert result.status == "filled"
    assert len(posts) == 1
    assert posts[0]["price"] == 1.80          # started at the package midpoint, not the natural


def test_open_spread_ladder_on_cancels_and_reprices_before_filling():
    from bot.features import S2bFeatures
    # first rung (1.80) never fills within the dwell -> must be cancel-confirmed, then the ladder
    # steps down a penny and the second rung (1.79) fills.
    http, posts, deletes = _ladder_http([["open", "open"], ["filled"]])
    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01), poll_s=0.01,
                      features=S2bFeatures(entry_price_ladder=True, entry_reprice_seconds=0.02,
                                          entry_max_work_seconds=5.0))
    result = deps.open_spread(_spread_payload(price=1.70, qty=2))
    assert result.status == "filled"
    assert [p["price"] for p in posts] == [1.80, 1.79]
    assert len(deletes) == 1   # the first (never-filled) rung's order was cancel-confirmed


def test_open_spread_ladder_on_never_crosses_below_min_credit():
    from bot.features import S2bFeatures
    # every rung stays open forever -> the ladder must give up once the next rung would cross
    # below the conservative floor (expected_executable_credit = max(natural=1.70, mid-slip=1.77)
    # = 1.77) rather than ever submitting 1.76 or lower.
    http, posts, deletes = _ladder_http([["open", "open"]] * 10)
    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01), poll_s=0.01,
                      features=S2bFeatures(entry_price_ladder=True, entry_reprice_seconds=0.02,
                                          entry_max_work_seconds=5.0))
    result = deps.open_spread(_spread_payload(price=1.70, qty=2))
    assert result.status != "filled"
    prices = [p["price"] for p in posts]
    assert min(prices) >= 1.77
    assert prices == [1.80, 1.79, 1.78, 1.77]


# ── Task 3.2: tp_price_ladder wired into close_spread (partner spec §7) ──────────────────────────

def _managed_position(qty=2):
    from bot.strategy.manage import ManagedPosition
    return ManagedPosition(ticker="SPY", short_strike=568.0, long_strike=558.0, credit=1.70,
                           qty=qty, expiry="2026-06-19")


def test_close_spread_ladder_off_is_exact_single_submit_behavior():
    from bot.strategy.manage import ExitAction
    # default S2bFeatures() -> tp_price_ladder=False (always the case for run_s2b_live.py) -- every
    # action (including TAKE_PROFIT) must be the pre-existing single-submit marketable-natural
    # close: ONE POST at short.ask - long.bid (3.50 - 1.60 = 1.90), no cancel/reprice machinery.
    for action in (ExitAction.TAKE_PROFIT, ExitAction.STOP, ExitAction.TIME_EXIT):
        http, posts, deletes = _ladder_http([["filled"]])
        deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                          get_vix_regime=lambda: (0.5, 0.01))
        assert deps.features.tp_price_ladder is False
        result = deps.close_spread(_managed_position(), action)
        assert result.status == "filled"
        assert len(posts) == 1
        assert posts[0]["price"] == 1.90          # marketable natural, untouched
        assert deletes == []


def test_close_spread_tp_ladder_on_starts_at_package_mid_debit_and_fills_first_rung():
    from bot.features import S2bFeatures
    from bot.strategy.manage import ExitAction
    # short 3.40/3.50 (mid 3.45), long 1.60/1.70 (mid 1.65) -> mid debit = 1.80, natural = 1.90
    http, posts, deletes = _ladder_http([["filled"]])
    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01), poll_s=0.01,
                      features=S2bFeatures(tp_price_ladder=True, entry_reprice_seconds=0.02,
                                          entry_max_work_seconds=5.0))
    result = deps.close_spread(_managed_position(), ExitAction.TAKE_PROFIT)
    assert result.status == "filled"
    assert len(posts) == 1
    assert posts[0]["price"] == 1.80          # started at the package mid debit, not the natural


def test_close_spread_tp_ladder_on_cancels_and_reprices_up_toward_natural_before_filling():
    from bot.features import S2bFeatures
    from bot.strategy.manage import ExitAction
    # first rung (1.80, the mid) never fills within the dwell -> must be cancel-confirmed, then the
    # ladder steps UP a penny (toward the 1.90 natural) and the second rung (1.81) fills.
    http, posts, deletes = _ladder_http([["open", "open"], ["filled"]])
    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01), poll_s=0.01,
                      features=S2bFeatures(tp_price_ladder=True, entry_reprice_seconds=0.02,
                                          entry_max_work_seconds=5.0))
    result = deps.close_spread(_managed_position(), ExitAction.TAKE_PROFIT)
    assert result.status == "filled"
    assert [p["price"] for p in posts] == [1.80, 1.81]
    assert len(deletes) == 1   # the first (never-filled) rung's order was cancel-confirmed


def test_close_spread_tp_ladder_on_never_exceeds_the_natural():
    from bot.features import S2bFeatures
    from bot.strategy.manage import ExitAction
    # every rung stays open forever -> the ladder must give up once the next rung would cross
    # above the natural (1.90) rather than ever paying 1.91 or more.
    http, posts, deletes = _ladder_http([["open", "open"]] * 20)
    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01), poll_s=0.01,
                      features=S2bFeatures(tp_price_ladder=True, entry_reprice_seconds=0.02,
                                          entry_max_work_seconds=5.0))
    result = deps.close_spread(_managed_position(), ExitAction.TAKE_PROFIT)
    assert result.status != "filled"
    prices = [p["price"] for p in posts]
    assert max(prices) <= 1.90
    assert prices == [round(1.80 + 0.01 * i, 2) for i in range(11)]   # 1.80..1.90 inclusive


def test_close_spread_stop_with_tp_ladder_on_still_uses_marketable_natural_single_submit():
    from bot.features import S2bFeatures
    from bot.strategy.manage import ExitAction
    # even with tp_price_ladder ON, STOP must never delay for price improvement (spec §7) -- it
    # stays the single marketable-natural submit, exactly like the flag-off path.
    http, posts, deletes = _ladder_http([["filled"]])
    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01),
                      features=S2bFeatures(tp_price_ladder=True))
    result = deps.close_spread(_managed_position(), ExitAction.STOP)
    assert result.status == "filled"
    assert len(posts) == 1
    assert posts[0]["price"] == 1.90
    assert deletes == []
