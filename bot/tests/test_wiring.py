from bot.app.wiring import reconcile_live
from bot.strategy.manage import ManagedPosition


def _pos(qty=2):
    return ManagedPosition("SPY", 568.0, 558.0, credit=3.0, qty=qty, expiry="2026-06-19")


def test_reconcile_live_clean_match():
    legs = {"SPY260619P00568000": -2, "SPY260619P00558000": 2}   # short -2, long +2
    r = reconcile_live([_pos(2)], legs, bot_equity=20_000.0, broker_equity=20_000.0)
    assert r.should_halt() is False


def test_reconcile_live_qty_mismatch_halts():
    legs = {"SPY260619P00568000": -1, "SPY260619P00558000": 1}   # broker only 1 lot, bot tracks 2
    r = reconcile_live([_pos(2)], legs, bot_equity=20_000.0, broker_equity=20_000.0)
    assert r.should_halt() is True


def test_reconcile_live_phantom_halts():
    r = reconcile_live([_pos(2)], {}, bot_equity=20_000.0, broker_equity=20_000.0)  # broker flat
    assert len(r.missing_at_broker) == 1 and r.should_halt() is True


def test_reconcile_live_untracked_leg_halts():
    legs = {"SPY260619P00568000": -2, "SPY260619P00558000": 2, "AAPL260619P00250000": -3}
    r = reconcile_live([_pos(2)], legs, bot_equity=20_000.0, broker_equity=20_000.0)
    assert len(r.untracked_at_broker) == 1 and r.should_halt() is True


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


def test_runner_stops_on_halt():
    def halting_tick(state, deps, now):
        state.halted = True
        return state
    calls = {"n": 0}
    def counting_tick(state, deps, now):
        calls["n"] += 1
        return halting_tick(state, deps, now)

    runner(BotState(), deps=None, now_fn=lambda: 0, sleep_fn=lambda s: None,
           poll_s=60, ticks=10, tick_fn=counting_tick)
    assert calls["n"] == 1   # halted after first tick -> stop


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
        "/markets/options/chains": chain,
        "/balances": {"balances": {"total_equity": 20_000.0}},
        "/positions": {"positions": "null"},
        "/markets/quotes": {"quotes": {"quote": {"symbol": "SPY", "bid": 574.9, "ask": 575.1, "last": 575.0}}},
    })
    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01))
    state = tick(BotState(), deps, datetime(2026, 6, 15, 10, 5))   # Monday 10:05
    assert len(state.open_positions) == 1 and state.open_positions[0].short_strike == 568.0
