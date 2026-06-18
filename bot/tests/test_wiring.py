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
from bot.app.orchestrator import BotState


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
