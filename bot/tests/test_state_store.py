from bot.app.state_store import save_state, load_state
from bot.app.orchestrator import BotState
from bot.strategy.manage import ManagedPosition


def _pos():
    return ManagedPosition("SPY", 720.0, 710.0, credit=1.71, qty=7, expiry="2026-07-02")


def test_save_load_roundtrip(tmp_path):
    p = str(tmp_path / "state.json")
    s = BotState(open_positions=[_pos()], halted=True, halt_reason="x", last_entry_date="2026-06-24")
    save_state(s, p)
    loaded = load_state(p)
    lp = loaded.open_positions[0]
    assert (lp.ticker, lp.short_strike, lp.long_strike, lp.credit, lp.qty, lp.expiry) == \
        ("SPY", 720.0, 710.0, 1.71, 7, "2026-07-02")
    assert loaded.halted is True and loaded.halt_reason == "x"
    assert loaded.last_entry_date == "2026-06-24"


def test_load_missing_returns_fresh(tmp_path):
    s = load_state(str(tmp_path / "nope.json"))
    assert s.open_positions == [] and s.halted is False and s.last_entry_date == ""


def test_roundtrips_entry_date_and_prev_flow_bias(tmp_path):
    p = str(tmp_path / "state.json")
    pos = ManagedPosition("SPY", 720.0, 710.0, 1.71, 7, "2026-07-02", entry_date="2026-06-30")
    s = BotState(open_positions=[pos], prev_flow_bias="bullish")
    save_state(s, p)
    loaded = load_state(p)
    assert loaded.open_positions[0].entry_date == "2026-06-30"
    assert loaded.prev_flow_bias == "bullish"


def test_old_state_file_missing_new_fields_defaults(tmp_path):
    # a state file written by the OLD bot (no entry_date / prev_flow_bias) must load with safe defaults
    import json
    p = str(tmp_path / "old_state.json")
    with open(p, "w") as f:
        json.dump({
            "open_positions": [{"ticker": "SPY", "short_strike": 720.0, "long_strike": 710.0,
                                "credit": 1.71, "qty": 7, "expiry": "2026-07-02"}],
            "halted": False, "halt_reason": "",
            "last_entry_date": "2026-06-24", "entries_today": 1,
        }, f)
    loaded = load_state(p)
    assert loaded.open_positions[0].entry_date == ""     # default keeps back-compat
    assert loaded.prev_flow_bias == ""
    assert loaded.last_entry_date == "2026-06-24" and loaded.entries_today == 1


def test_runner_persists_state_each_tick(tmp_path):
    from bot.app.wiring import runner
    p = str(tmp_path / "state.json")

    def fake_tick(state, deps, now):
        state.open_positions.append(_pos())   # the tick opens a position
        return state

    runner(BotState(), deps=None, now_fn=lambda: 0, sleep_fn=lambda s: None,
           poll_s=60, ticks=1, tick_fn=fake_tick, state_path=p)
    reloaded = load_state(p)
    assert len(reloaded.open_positions) == 1   # persisted after the tick -> survives a restart
