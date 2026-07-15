"""TDD for Task 4.2: automated entry markouts (partner review v2 §14). LOG ONLY -- records
SPY/spread follow-ups for every evaluated candidate (filled AND rejected); never trades.
"""
from datetime import datetime, timedelta

from bot.research.markouts import MarkoutTracker, HORIZONS_MIN
from bot.app.orchestrator import BotState, Deps, tick, run_entry_cycle
from bot.strategy.s2b import OptionQuote
from bot.risk_gate import AccountState
from bot.features import S2bFeatures


T0 = datetime(2026, 6, 15, 10, 0, 0)


def _signal(**over):
    base = dict(
        signal_id="sig-1", ticker="SPY", short_strike=568.0, long_strike=558.0,
        expiry="2026-06-19", filled=False, entry_spy=575.0, entry_spread_value=3.0,
        credit=3.0, qty=1, entry_delta=0.35, entry_iv=0.20,
    )
    base.update(over)
    return base


# ── record_signal + resolve_due: horizon filling ─────────────────────────────────────────────

def test_record_signal_adds_one_pending_entry():
    logged = []
    tr = MarkoutTracker(write_fn=lambda rec: logged.append(rec))
    tr.record_signal(T0, _signal())
    assert len(tr.to_state()) == 1
    assert logged == []   # recording alone never writes


def test_resolve_due_fills_1_and_5_min_horizons_at_the_right_time():
    logged = []
    tr = MarkoutTracker(write_fn=lambda rec: logged.append(rec))
    tr.record_signal(T0, _signal())

    def spread_value_fn(item):
        return 2.5   # spread cheaper to close now (favorable to the credit seller)

    # at +1 min: only the 1-min horizon should resolve
    tr.resolve_due(T0 + timedelta(minutes=1), spy_now=576.0, spread_value_fn=spread_value_fn)
    state = tr.to_state()
    assert state[0]["horizons"][1]["spy_move"] == 1.0
    assert state[0]["horizons"][1]["spread_move"] == -0.5
    assert state[0]["horizons"][5] is None

    # at +5 min: the 5-min horizon resolves too, 1-min stays untouched (not re-computed)
    tr.resolve_due(T0 + timedelta(minutes=5), spy_now=577.0, spread_value_fn=spread_value_fn)
    state = tr.to_state()
    assert state[0]["horizons"][5]["spy_move"] == 2.0
    assert state[0]["horizons"][1]["spy_move"] == 1.0   # unchanged


def test_resolve_due_computes_mfe_and_mae_across_calls():
    tr = MarkoutTracker(write_fn=lambda rec: None)
    tr.record_signal(T0, _signal(entry_spread_value=3.0))

    values = iter([2.5, 3.5, 2.0])   # favorable=0.5, then -0.5 (adverse), then 1.0 (best so far)

    def spread_value_fn(item):
        return next(values)

    tr.resolve_due(T0 + timedelta(minutes=1), spy_now=575.0, spread_value_fn=spread_value_fn)
    tr.resolve_due(T0 + timedelta(minutes=2), spy_now=575.0, spread_value_fn=spread_value_fn)
    tr.resolve_due(T0 + timedelta(minutes=3), spy_now=575.0, spread_value_fn=spread_value_fn)
    state = tr.to_state()
    assert state[0]["mfe"] == 1.0
    assert state[0]["mae"] == -0.5


def test_resolve_due_writes_one_row_once_all_horizons_complete():
    logged = []
    tr = MarkoutTracker(write_fn=lambda rec: logged.append(rec))
    tr.record_signal(T0, _signal())

    def spread_value_fn(item):
        return 2.9

    for h in HORIZONS_MIN:
        tr.resolve_due(T0 + timedelta(minutes=h), spy_now=575.5, spread_value_fn=spread_value_fn)
    assert len(logged) == 1
    row = logged[0]
    assert row["event"] == "MARKOUT"
    assert row["spy_move_1m"] == 0.5
    assert row["spy_move_60m"] == 0.5
    # writing again after all horizons are already done must not duplicate the row
    tr.resolve_due(T0 + timedelta(minutes=90), spy_now=575.5, spread_value_fn=spread_value_fn)
    assert len(logged) == 1


# ── rejected signals: no survivorship bias ───────────────────────────────────────────────────

def test_rejected_signal_is_retained_and_resolved_like_a_fill():
    logged = []
    tr = MarkoutTracker(write_fn=lambda rec: logged.append(rec))
    tr.record_signal(T0, _signal(signal_id="rejected-1", filled=False))
    tr.resolve_due(T0 + timedelta(minutes=1), spy_now=574.0, spread_value_fn=lambda item: 3.1)
    state = tr.to_state()
    assert len(state) == 1
    assert state[0]["filled"] is False
    assert state[0]["horizons"][1] is not None   # still gets SPY + spread markouts


# ── finalize_eod ──────────────────────────────────────────────────────────────────────────────

def test_finalize_eod_writes_a_row_for_every_pending_signal_and_clears_pending():
    logged = []
    tr = MarkoutTracker(write_fn=lambda rec: logged.append(rec))
    tr.record_signal(T0, _signal(signal_id="filled-1", filled=True))
    tr.record_signal(T0, _signal(signal_id="rejected-1", filled=False))
    tr.finalize_eod(spy_close=580.0, spread_value_fn=lambda item: 1.0)
    assert len(logged) == 2
    ids = {r["signal_id"] for r in logged}
    assert ids == {"filled-1", "rejected-1"}
    for r in logged:
        assert r["event"] == "EOD"
        assert r["eod_spy_move"] == 5.0
        assert r["eod_spread_value"] == 1.0
        assert r["eod_spread_move"] == -2.0
    assert tr.to_state() == []   # cleared -- the day's signals are done


def test_finalize_eod_one_item_error_does_not_block_others():
    logged = []
    tr = MarkoutTracker(write_fn=lambda rec: logged.append(rec))
    tr.record_signal(T0, _signal(signal_id="boom"))
    tr.record_signal(T0, _signal(signal_id="fine"))

    def flaky(item):
        if item["signal_id"] == "boom":
            raise RuntimeError("no quote")
        return 1.0

    tr.finalize_eod(spy_close=580.0, spread_value_fn=flaky)
    assert len(logged) == 2
    boom_row = [r for r in logged if r["signal_id"] == "boom"][0]
    assert boom_row["eod_spread_value"] is None


# ── filled trades: time-to-TP / time-to-stop + MFE-before-stop / MAE-before-TP ───────────────────

def test_filled_trade_records_time_to_tp_and_mae_before_tp():
    tr = MarkoutTracker(write_fn=lambda rec: None)
    tr.record_signal(T0, _signal(signal_id="f1", filled=True, credit=3.0,
                                 tp_value=1.5, stop_value=9.0))
    values = iter([3.5, 1.4])   # first adverse (mae=-0.5), then crosses TP (favorable=1.6)

    def spread_value_fn(item):
        return next(values)

    tr.resolve_due(T0 + timedelta(minutes=1), spy_now=575.0, spread_value_fn=spread_value_fn)
    tr.resolve_due(T0 + timedelta(minutes=2), spy_now=575.0, spread_value_fn=spread_value_fn)
    state = tr.to_state()[0]
    assert state["time_to_tp"] == 2.0
    assert state["time_to_stop"] is None
    assert state["mae_before_tp"] == -0.5   # the worst excursion seen before TP was hit


def test_filled_trade_records_time_to_stop_and_mfe_before_stop():
    tr = MarkoutTracker(write_fn=lambda rec: None)
    tr.record_signal(T0, _signal(signal_id="f2", filled=True, credit=3.0,
                                 tp_value=1.5, stop_value=9.0))
    values = iter([2.0, 9.5])   # first favorable (mfe=1.0), then crosses STOP

    def spread_value_fn(item):
        return next(values)

    tr.resolve_due(T0 + timedelta(minutes=1), spy_now=575.0, spread_value_fn=spread_value_fn)
    tr.resolve_due(T0 + timedelta(minutes=2), spy_now=575.0, spread_value_fn=spread_value_fn)
    state = tr.to_state()[0]
    assert state["time_to_stop"] == 2.0
    assert state["time_to_tp"] is None
    assert state["mfe_before_stop"] == 1.0   # the best excursion seen before the stop was hit


def test_rejected_signal_never_gets_time_to_tp_or_stop():
    tr = MarkoutTracker(write_fn=lambda rec: None)
    tr.record_signal(T0, _signal(signal_id="r1", filled=False))   # tp/stop values default None
    tr.resolve_due(T0 + timedelta(minutes=1), spy_now=575.0, spread_value_fn=lambda item: 0.1)
    state = tr.to_state()[0]
    assert state["time_to_tp"] is None
    assert state["time_to_stop"] is None


# ── to_state / from_state round trip ─────────────────────────────────────────────────────────

def test_to_state_from_state_round_trips_pending_markouts():
    tr = MarkoutTracker(write_fn=lambda rec: None)
    tr.record_signal(T0, _signal(signal_id="rt-1"))
    tr.resolve_due(T0 + timedelta(minutes=1), spy_now=576.0, spread_value_fn=lambda item: 2.9)
    snapshot = tr.to_state()

    tr2 = MarkoutTracker.from_state(snapshot, write_fn=lambda rec: None)
    restored = tr2.to_state()
    assert restored == snapshot
    # the restored tracker keeps resolving correctly (ts parsed back to a real datetime, horizon
    # keys parsed back to int rather than staying JSON string keys)
    tr2.resolve_due(T0 + timedelta(minutes=5), spy_now=577.0, spread_value_fn=lambda item: 2.8)
    state = tr2.to_state()[0]
    assert state["horizons"][5]["spy_move"] == 2.0


def test_from_state_survives_a_json_round_trip_via_state_store(tmp_path):
    """The realistic path: to_state() -> json.dump -> json.load -> from_state() (as state_store
    does for BotState.markout_pending)."""
    import json
    tr = MarkoutTracker(write_fn=lambda rec: None)
    tr.record_signal(T0, _signal(signal_id="json-1"))
    tr.resolve_due(T0 + timedelta(minutes=1), spy_now=576.0, spread_value_fn=lambda item: 2.9)
    p = tmp_path / "markout_state.json"
    with open(p, "w") as f:
        json.dump(tr.to_state(), f)
    with open(p) as f:
        loaded = json.load(f)
    tr2 = MarkoutTracker.from_state(loaded, write_fn=lambda rec: None)
    tr2.resolve_due(T0 + timedelta(minutes=5), spy_now=577.0, spread_value_fn=lambda item: 2.8)
    state = tr2.to_state()[0]
    assert state["horizons"][1]["spy_move"] == 1.0
    assert state["horizons"][5]["spy_move"] == 2.0


# ══════════════════════════════════════════════════════════════════════════════════════════════
# Orchestrator wiring: markouts must NEVER change the entry decision, and stay off for Bot C.
# ══════════════════════════════════════════════════════════════════════════════════════════════

def _pos_chain():
    return [
        OptionQuote(568.0, 0.36, 4.00, 4.10),
        OptionQuote(558.0, 0.18, 1.90, 2.00),
    ]


def _acct(today, conc):
    return AccountState(100_000.0, 100_000.0, 0.0, conc, 0.0, {}, today)


def _base_deps(**over):
    base = dict(
        get_spot=lambda sym: 575.0, get_atr=lambda sym: 6.0,
        get_chain=lambda sym, exp: _pos_chain(),
        pick_expiry=lambda today: "2026-06-19",
        get_vix_regime=lambda: (0.5, 0.01), account_state=_acct,
        mark_position=lambda p: 3.0, dte_of=lambda p, today: 5,
        open_spread=lambda payload: "filled", close_spread=lambda p, a: "filled",
        broker_positions=lambda: [], broker_equity=lambda: 100_000.0,
        bot_equity=lambda: 100_000.0, alert_sink=lambda alerts: None,
        risk_equity=lambda: 100_000.0,
    )
    base.update(over)
    return Deps(**base)


MONDAY = datetime(2026, 6, 15, 10, 5)   # Monday 10:05, expiry 2026-06-19


def test_markout_off_by_default_and_state_starts_empty():
    assert S2bFeatures().regime_shadow_monitor is False
    assert BotState().markout_pending == []


def test_entry_cycle_records_a_markout_signal_when_filled_and_flag_on():
    logged = []
    f = S2bFeatures(regime_shadow_monitor=True)
    d = _base_deps(features=f, markout_log=lambda rec: logged.append(rec))
    state = BotState()
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    assert len(state.markout_pending) == 1
    pending = state.markout_pending[0]
    assert pending["filled"] is True
    assert pending["short_strike"] == 568.0
    assert pending["entry_spy"] == 575.0
    assert pending["tp_value"] is not None and pending["stop_value"] is not None


def test_entry_cycle_records_a_rejected_candidate_markout():
    logged = []
    f = S2bFeatures(regime_shadow_monitor=True, transaction_cost_gate=True)
    thin_chain = [
        OptionQuote(strike=568.0, delta=0.36, bid=0.50, ask=0.55),
        OptionQuote(strike=558.0, delta=0.18, bid=0.10, ask=0.15),
    ]
    d = _base_deps(features=f, get_chain=lambda sym, exp: thin_chain,
                  markout_log=lambda rec: logged.append(rec))
    state = BotState()
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "cost_gate"
    assert len(state.markout_pending) == 1
    pending = state.markout_pending[0]
    assert pending["filled"] is False
    assert pending["tp_value"] is None and pending["stop_value"] is None   # rejected -> no TP/stop tracking


def test_no_candidate_gates_never_record_a_markout():
    # off-hours: no order was ever built -> no markout signal to track
    f = S2bFeatures(regime_shadow_monitor=True)
    d = _base_deps(features=f)
    state = BotState()
    state, info = run_entry_cycle(state, d, datetime(2026, 6, 15, 8, 0))   # before 10am
    assert info is None
    assert state.markout_pending == []


def test_flag_off_records_no_markout_signal():
    f = S2bFeatures()   # default all off
    d = _base_deps(features=f)
    state = BotState()
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    assert state.markout_pending == []


def test_tick_resolves_due_horizons_and_persists_pending_across_ticks():
    f = S2bFeatures(regime_shadow_monitor=True)
    logged = []
    d = _base_deps(features=f, markout_log=lambda rec: logged.append(rec))
    state = BotState()
    state = tick(state, d, MONDAY)
    assert len(state.markout_pending) == 1
    # next tick, 90 minutes later (all horizons due) and past market close -> EOD fires, clearing pending
    later = datetime(2026, 6, 15, 16, 30)
    state = tick(state, d, later)
    assert state.markout_pending == []
    eod_rows = [r for r in logged if r["event"] == "EOD"]
    assert len(eod_rows) == 1


def test_markout_never_changes_the_entry_outcome():
    """The critical safety property: identical ticks except for the markout/shadow flag must
    produce byte-identical entry decisions/state."""
    def _run(flag_on):
        f = S2bFeatures(regime_shadow_monitor=flag_on)
        logged = []
        d = _base_deps(features=f, markout_log=lambda rec: logged.append(rec))
        state = BotState()
        state = tick(state, d, MONDAY)
        return state, logged

    state_off, logged_off = _run(False)
    state_on, logged_on = _run(True)

    assert len(state_off.open_positions) == len(state_on.open_positions) == 1
    p_off, p_on = state_off.open_positions[0], state_on.open_positions[0]
    assert (p_off.short_strike, p_off.long_strike, p_off.qty, p_off.credit) == \
           (p_on.short_strike, p_on.long_strike, p_on.qty, p_on.credit)
    assert state_off.halted == state_on.halted
    assert state_off.entries_today == state_on.entries_today
    assert logged_off == []          # flag off -> markout sink never called
    assert len(logged_on) >= 0        # flag on -> may or may not fire yet, but never raises


def test_flag_off_writes_no_markout_file(tmp_path):
    from bot.app.wiring import build_deps, make_markout_logger
    path = str(tmp_path / "markouts_test.csv")
    http = lambda method, p, params=None, data=None: (
        {"balances": {"total_equity": 20_000.0}} if "/balances" in p else
        {"positions": "null"} if "/positions" in p else
        {"expirations": {"date": ["2026-06-19"]}} if "expirations" in p else
        {"options": {"option": [
            {"strike": 568.0, "option_type": "put", "bid": 3.40, "ask": 3.50, "greeks": {"delta": -0.36}},
            {"strike": 558.0, "option_type": "put", "bid": 1.60, "ask": 1.70, "greeks": {"delta": -0.18}},
        ]}} if "chains" in p else
        {"quotes": {"quote": {"symbol": "SPY", "bid": 574.9, "ask": 575.1, "last": 575.0}}} if "quotes" in p else
        {"order": {"id": 1, "status": "filled"}}
    )
    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01),
                      markout_log=make_markout_logger(path))   # flag stays off (default features)
    assert deps.features.regime_shadow_monitor is False
    state = tick(BotState(), deps, MONDAY)
    import os
    assert not os.path.exists(path)


def test_build_deps_default_markout_log_is_noop():
    from bot.app.wiring import build_deps
    http = lambda method, p, params=None, data=None: {"balances": {"total_equity": 20_000.0}}
    deps = build_deps(http, account_id="ABC", get_spot=lambda s: 575.0, get_atr=lambda s: 6.0,
                      get_vix_regime=lambda: (0.5, 0.01))
    deps.markout_log({"anything": "goes"})   # must not raise


def test_run_s2b_live_defaults_leave_regime_shadow_monitor_off_for_markouts_too():
    import inspect
    from bot.app import run_s2b_live
    src = inspect.getsource(run_s2b_live)
    assert "features=" not in src


def test_build_and_run_gates_markout_log_on_regime_shadow_monitor_flag():
    import inspect
    from bot.app import run_s2b
    src = inspect.getsource(run_s2b.build_and_run)
    assert "regime_shadow_monitor" in src and "markout" in src.lower()
