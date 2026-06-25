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
