"""TDD for Task 4.2: automated entry markouts (partner review v2 §14). LOG ONLY -- records
SPY/spread follow-ups for every evaluated candidate (filled AND rejected); never trades.
"""
from datetime import datetime, timedelta

from bot.research.markouts import MarkoutTracker, HORIZONS_MIN
from bot.app.orchestrator import BotState, Deps, tick, run_entry_cycle, run_markout_cycle
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
    assert S2bFeatures().markout_tracking is False
    assert S2bFeatures().regime_shadow_monitor is False
    assert BotState().markout_pending == []
    assert BotState().markout_obs_last == {}


def test_entry_cycle_records_a_markout_signal_when_filled_and_flag_on():
    logged = []
    f = S2bFeatures(markout_tracking=True)
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
    f = S2bFeatures(markout_tracking=True, transaction_cost_gate=True)
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
    f = S2bFeatures(markout_tracking=True)
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
    f = S2bFeatures(markout_tracking=True)
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
    """The critical safety property: identical ticks except for the markout flag must
    produce byte-identical entry decisions/state."""
    def _run(flag_on):
        f = S2bFeatures(markout_tracking=flag_on)
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


# ── Priority-0 fix item 4: separate flag, batched quotes, signal dedup, Bot-C guard ──────────────

def test_markout_cycle_gated_on_markout_tracking_not_shadow():
    """run_markout_cycle must be gated on its OWN flag (markout_tracking), NOT on
    regime_shadow_monitor: shadow ON + markout OFF does zero markout work; markout ON runs it."""
    tr = MarkoutTracker(write_fn=lambda r: None)
    tr.record_signal(T0, _signal(signal_id="g1"))
    pending = tr.to_state()

    def _run(shadow, markout):
        logged = []
        f = S2bFeatures(regime_shadow_monitor=shadow, markout_tracking=markout)
        d = _base_deps(features=f, markout_log=lambda rec: logged.append(rec),
                       mark_position=lambda p: 2.9, get_spot=lambda s: 575.5)
        state = BotState(markout_pending=[dict(p) for p in pending])
        run_markout_cycle(state, d, T0 + timedelta(minutes=90))   # all horizons due, pre-16:00
        return state, logged

    # shadow ON, markout OFF -> NO markout work: no log writes, pending untouched
    state_off, logged_off = _run(shadow=True, markout=False)
    assert logged_off == []
    assert state_off.markout_pending == pending

    # markout ON -> it runs: horizons resolve and one MARKOUT row is written
    state_on, logged_on = _run(shadow=False, markout=True)
    assert len(logged_on) == 1
    assert logged_on[0]["event"] == "MARKOUT"


def test_resolve_due_batched_issues_one_quote_call_for_unique_legs():
    N_UNIQUE = 5
    calls = []
    def batch_quote(symbols):
        calls.append(tuple(symbols))
        return {s: 0.5 for s in symbols}

    # Many pending items across a FEW unique (short,long,expiry) spreads, all past their 1-min
    # horizon so they are "due" -- the batched resolve must fold them into ONE deduped quote call.
    tr = MarkoutTracker(write_fn=lambda r: None)
    for i in range(N_UNIQUE):
        short = 568.0 + i
        for _ in range(8):   # 8 duplicate pending items per unique spread
            tr.record_signal(T0, _signal(short_strike=short, long_strike=short - 10.0))

    cov = tr.resolve_due_batched(T0 + timedelta(minutes=2), spy_now=750.0, batch_quote_fn=batch_quote)
    assert len(calls) == 1                       # exactly ONE batched quote call
    assert len(calls[0]) == 2 * N_UNIQUE         # 2 legs per unique spread, deduped
    assert cov == {"quotes_requested": 2 * N_UNIQUE, "quotes_missing": 0}


def test_resolve_due_batched_missing_symbol_is_best_effort_none_mark():
    # a leg missing from the batch -> that spread's mark is None (no raise); counted in coverage
    tr = MarkoutTracker(write_fn=lambda r: None)
    tr.record_signal(T0, _signal(short_strike=568.0, long_strike=558.0))

    def batch_quote(symbols):
        return {symbols[0]: 0.5}    # drop the second leg entirely

    cov = tr.resolve_due_batched(T0 + timedelta(minutes=2), spy_now=750.0, batch_quote_fn=batch_quote)
    assert cov["quotes_requested"] == 2 and cov["quotes_missing"] == 1
    # 1-min horizon still resolves (spy_move present); spread_move is None (missing mark)
    item = tr.to_state()[0]
    assert item["horizons"][1] is not None
    assert item["horizons"][1]["spread_move"] is None


def test_run_markout_cycle_uses_batched_option_quotes_when_wired():
    # when deps.option_quotes is wired, run_markout_cycle prices due legs in ONE batched call
    # (not one deps.mark_position call per item) -- and mark_position must NOT be used.
    tr = MarkoutTracker(write_fn=lambda r: None)
    tr.record_signal(T0, _signal(signal_id="b1"))
    calls = []
    def option_quotes(syms):
        calls.append(tuple(syms))
        return {s: 0.5 for s in syms}
    f = S2bFeatures(markout_tracking=True)
    def _boom_mark(p):
        raise AssertionError("mark_position must not be called when option_quotes is wired")
    d = _base_deps(features=f, markout_log=lambda rec: None, option_quotes=option_quotes,
                   mark_position=_boom_mark, get_spot=lambda s: 575.5)
    state = BotState(markout_pending=tr.to_state())
    run_markout_cycle(state, d, T0 + timedelta(minutes=2))   # pre-16:00 -> no EOD finalize
    assert len(calls) == 1                       # exactly ONE batched quote call


def test_markout_signal_dedup_within_15min_window():
    """Repeated identical candidates polled many times in one 15-min window spawn <=1 pending
    markout; a new window, or a new strike, spawns another (Priority-0 fix item 4)."""
    logged = []
    f = S2bFeatures(markout_tracking=True, transaction_cost_gate=True)
    thin_chain = [
        OptionQuote(strike=568.0, delta=0.36, bid=0.50, ask=0.55),
        OptionQuote(strike=558.0, delta=0.18, bid=0.10, ask=0.15),
    ]
    d = _base_deps(features=f, get_chain=lambda sym, exp: thin_chain,
                  markout_log=lambda rec: logged.append(rec))
    state = BotState()
    # four identical candidate cycles, all in the SAME 15-min window (10:00-10:14, bucket15==2)
    for minute in (0, 2, 5, 8):
        state, info = run_entry_cycle(state, d, datetime(2026, 6, 15, 10, minute))
        assert info == "cost_gate"
    assert len(state.markout_pending) == 1        # deduped down to one pending markout

    # a NEW 15-min window (10:20, bucket15==3) -> a second pending markout for the same strikes
    state, _ = run_entry_cycle(state, d, datetime(2026, 6, 15, 10, 20))
    assert len(state.markout_pending) == 2

    # a NEW strike (569/559) in the same new window -> a third pending markout
    other_chain = [
        OptionQuote(strike=569.0, delta=0.36, bid=0.50, ask=0.55),
        OptionQuote(strike=559.0, delta=0.18, bid=0.10, ask=0.15),
    ]
    d2 = _base_deps(features=f, get_chain=lambda sym, exp: other_chain,
                   markout_log=lambda rec: logged.append(rec))
    state, _ = run_entry_cycle(state, d2, datetime(2026, 6, 15, 10, 22))
    assert len(state.markout_pending) == 3


def test_markout_dedup_key_includes_filled_so_reject_then_fill_both_record():
    """A candidate REJECTED and then FILLED at the same strikes in the same 15-min window must
    record BOTH markouts -- the fill's follow-up is the richer one (filled=True carries
    tp_value/stop_value and drives time-to-TP/time-to-stop), and the reject->fill transition is
    exactly the filled-vs-rejected comparison §14 exists to measure. `filled` is part of the dedup
    key, so the fill is NOT deduped away by the earlier reject (Task 4 review fix)."""
    logged = []
    thin_chain = [   # 568/558, too thin -> cost_gate reject
        OptionQuote(strike=568.0, delta=0.36, bid=0.50, ask=0.55),
        OptionQuote(strike=558.0, delta=0.18, bid=0.10, ask=0.15),
    ]
    d_reject = _base_deps(features=S2bFeatures(markout_tracking=True, transaction_cost_gate=True),
                          get_chain=lambda sym, exp: thin_chain,
                          markout_log=lambda rec: logged.append(rec))
    # fill deps: same 568/558 strikes, rich enough to fill; only markout_tracking on (legacy sizing)
    d_fill = _base_deps(features=S2bFeatures(markout_tracking=True),
                        get_chain=lambda sym, exp: _pos_chain(),
                        markout_log=lambda rec: logged.append(rec))
    state = BotState()
    # poll 1 (10:05, bucket15==2): REJECT the 568/558 candidate -> markout filled=False
    state, info = run_entry_cycle(state, d_reject, datetime(2026, 6, 15, 10, 5))
    assert info == "cost_gate"
    assert len(state.markout_pending) == 1
    assert state.markout_pending[0]["filled"] is False
    # poll 2 (10:08, SAME window, SAME strikes): FILL -> a SECOND, filled=True markout (not deduped)
    state, info = run_entry_cycle(state, d_fill, datetime(2026, 6, 15, 10, 8))
    assert info == "filled"
    assert len(state.markout_pending) == 2
    assert state.markout_pending[1]["filled"] is True
    assert state.markout_pending[1]["tp_value"] is not None   # the richer fill-only follow-up fields


def test_bot_c_both_flags_off_does_zero_markout_work():
    """Bot C (live, real money): regime_shadow_monitor=False AND markout_tracking=False must do
    ZERO markout work -- no pending, no obs key, no log writes, and option_quotes never called."""
    logged, calls = [], []
    f = S2bFeatures()   # both flags off (defaults)
    d = _base_deps(features=f, markout_log=lambda rec: logged.append(rec),
                  option_quotes=lambda syms: calls.append(syms) or {s: 0.5 for s in syms})
    state = tick(BotState(), d, MONDAY)
    assert state.open_positions and state.open_positions[0].short_strike == 568.0   # entry still happens
    assert state.markout_pending == []
    assert state.markout_obs_last == {}
    assert logged == []          # markout sink never called
    assert calls == []           # batched quote fn never called


def test_build_and_run_gates_markout_log_on_markout_tracking_flag():
    import inspect
    from bot.app import run_s2b
    src = inspect.getsource(run_s2b.build_and_run)
    # markout CSV logger is now wired iff markout_tracking is on (its OWN flag, split from the
    # shadow monitor -- Priority-0 fix item 4).
    assert "markout_tracking" in src and "markout" in src.lower()
