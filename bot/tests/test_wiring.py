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
                      features=S2bFeatures(est_commission_per_leg_rt=0.70))
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
                      features=S2bFeatures(est_commission_per_leg_rt=0.70))
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
