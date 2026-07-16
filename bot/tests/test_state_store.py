import pytest

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


def test_roundtrips_realized_today_and_risk_day(tmp_path):
    p = str(tmp_path / "state.json")
    s = BotState(realized_today=-345.67, risk_day="2026-07-10")
    save_state(s, p)
    loaded = load_state(p)
    assert loaded.realized_today == pytest.approx(-345.67)
    assert loaded.risk_day == "2026-07-10"


def test_old_state_file_missing_daily_gate_fields_defaults(tmp_path):
    # a state file written before the daily-risk gate existed (no realized_today/risk_day)
    # must load with safe defaults (fresh-day, zero realized P&L).
    import json
    p = str(tmp_path / "old_state.json")
    with open(p, "w") as f:
        json.dump({
            "open_positions": [], "halted": False, "halt_reason": "",
            "last_entry_date": "2026-06-24", "entries_today": 1,
        }, f)
    loaded = load_state(p)
    assert loaded.realized_today == 0.0
    assert loaded.risk_day == ""


def test_roundtrips_markout_pending_and_seq(tmp_path):
    # partner review v2 §14: pending research markouts must survive a restart (they resolve
    # 1-60 min after the signal, which can straddle a restart).
    from bot.research.markouts import MarkoutTracker
    from datetime import datetime
    p = str(tmp_path / "state.json")
    tr = MarkoutTracker(write_fn=lambda rec: None)
    tr.record_signal(datetime(2026, 6, 15, 10, 5), {
        "signal_id": "2026-06-15-1", "ticker": "SPY", "short_strike": 568.0, "long_strike": 558.0,
        "expiry": "2026-06-19", "filled": True, "entry_spy": 575.0, "entry_spread_value": 3.0,
        "credit": 3.0, "qty": 1,
    })
    obs = {"date": "2026-06-15", "expiry": "2026-06-19", "short": 568.0, "long": 558.0, "bucket15": 2}
    s = BotState(markout_pending=tr.to_state(), markout_seq=1, markout_obs_last=obs)
    save_state(s, p)
    loaded = load_state(p)
    assert loaded.markout_seq == 1
    assert loaded.markout_obs_last == obs
    assert len(loaded.markout_pending) == 1
    # the restored pending list must still resolve correctly (ts/horizon-keys parsed back)
    tr2 = MarkoutTracker.from_state(loaded.markout_pending, write_fn=lambda rec: None)
    tr2.resolve_due(datetime(2026, 6, 15, 10, 6), spy_now=576.0, spread_value_fn=lambda item: 2.9)
    restored_item = tr2.to_state()[0]
    assert restored_item["horizons"][1]["spy_move"] == 1.0


def test_old_state_file_missing_markout_fields_defaults(tmp_path):
    # a state file written before Task 4.2 existed (no markout_pending/markout_seq) must load
    # with safe defaults (empty pending, zero counter).
    import json
    p = str(tmp_path / "old_state.json")
    with open(p, "w") as f:
        json.dump({
            "open_positions": [], "halted": False, "halt_reason": "",
            "last_entry_date": "2026-06-24", "entries_today": 1,
        }, f)
    loaded = load_state(p)
    assert loaded.markout_pending == []
    assert loaded.markout_seq == 0
    assert loaded.markout_obs_last == {}
    assert loaded.credit_obs_last == {}          # Fix C: absent in an old file -> {} default, no KeyError


def test_roundtrips_credit_obs_last(tmp_path):
    # Fix C: the item-3 credit-tier dedup anchor (last RECORDED observation per DTE bucket) must
    # survive a restart, or the first post-restart poll would re-record a duplicate observation and
    # skew the adaptive credit threshold. Mirrors the markout_obs_last coverage above.
    p = str(tmp_path / "state.json")
    obs = {"bucket_1": {"date": "2026-06-15", "expiry": "2026-06-19", "short": 568.0,
                        "long": 558.0, "ratio": 0.17, "bucket15": 2}}
    s = BotState(credit_obs_last=obs)
    save_state(s, p)
    loaded = load_state(p)
    assert loaded.credit_obs_last == obs


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
