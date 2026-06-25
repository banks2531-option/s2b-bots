from datetime import datetime
from bot.app.orchestrator import BotState, Deps
from bot.app.orchestrator import run_reconcile_cycle, run_management_cycle, run_entry_cycle, tick
from bot.strategy.manage import ManagedPosition
from bot.strategy.s2b import OptionQuote
from bot.risk_gate import AccountState


def test_botstate_defaults():
    s = BotState()
    assert s.open_positions == [] and s.halted is False and s.halt_reason == ""


def test_deps_is_constructible_with_callables():
    d = Deps(
        get_spot=lambda sym: 575.0, get_atr=lambda sym: 6.0,
        get_chain=lambda sym, exp: [], pick_expiry=lambda today: "2026-06-19",
        get_vix_regime=lambda: (0.5, 0.01), account_state=lambda today, conc: None,
        mark_position=lambda p: 3.0, dte_of=lambda p, today: 5,
        open_spread=lambda payload: "filled", close_spread=lambda p, a: "filled",
        broker_positions=lambda: [], broker_equity=lambda: 20_000.0,
        bot_equity=lambda: 20_000.0, alert_sink=lambda alerts: None,
    )
    assert d.base_risk_pct == 0.10


def _pos():
    return ManagedPosition("SPY", 568.0, 558.0, credit=3.0, qty=1, expiry="2026-06-19")


def _deps(**over):
    base = dict(
        get_spot=lambda sym: 575.0, get_atr=lambda sym: 6.0,
        get_chain=lambda sym, exp: [], pick_expiry=lambda today: "2026-06-19",
        get_vix_regime=lambda: (0.5, 0.01), account_state=lambda today, conc: None,
        mark_position=lambda p: 3.0, dte_of=lambda p, today: 5,
        open_spread=lambda payload: "filled", close_spread=lambda p, a: "filled",
        broker_positions=lambda: [], broker_equity=lambda: 20_000.0,
        bot_equity=lambda: 20_000.0, alert_sink=lambda alerts: None,
    )
    base.update(over)
    return Deps(**base)


def test_reconcile_clean_no_halt():
    state = BotState(open_positions=[_pos()])
    d = _deps(broker_positions=lambda: [_pos()])
    state, drift = run_reconcile_cycle(state, d)
    assert state.halted is False


def test_reconcile_drift_halt_auto_clears_when_clean():
    # A reconcile-drift halt is transient: once a later reconcile comes back clean (broker truth
    # re-syncs), the bot resumes entries on its own — no operator intervention, no process restart.
    state = BotState(open_positions=[_pos()], halted=True, halt_reason="reconcile drift")
    d = _deps(broker_positions=lambda: [_pos()])      # broker now matches -> clean
    state, _ = run_reconcile_cycle(state, d)
    assert state.halted is False and state.halt_reason == ""


def test_reconcile_clean_does_not_clear_failed_close_halt():
    # A clean reconcile must NOT auto-clear a more serious "failed close" halt — only reconcile
    # drift self-heals; a failed exit order stays halted until an operator investigates.
    state = BotState(open_positions=[_pos()], halted=True, halt_reason="failed close")
    d = _deps(broker_positions=lambda: [_pos()])
    state, _ = run_reconcile_cycle(state, d)
    assert state.halted is True and state.halt_reason == "failed close"


def test_reconcile_untracked_broker_position_halts_and_alerts():
    sent = []
    state = BotState(open_positions=[])               # bot thinks flat
    d = _deps(broker_positions=lambda: [_pos()],      # broker still holds one
              alert_sink=lambda alerts: sent.append(alerts))
    state, drift = run_reconcile_cycle(state, d)
    assert state.halted is True and "reconcile" in state.halt_reason
    assert sent and sent[0][0].message  # a critical alert was emitted


def test_management_closes_filled_position_and_removes_it():
    pos = _pos()
    state = BotState(open_positions=[pos])
    d = _deps(mark_position=lambda p: 10.0,           # past stop
              close_spread=lambda p, a: "filled")
    state, results = run_management_cycle(state, d, today="2026-06-17")
    assert len(results) == 1 and results[0].action.value == "stop"
    assert state.open_positions == []                 # filled close -> removed
    assert state.halted is False


def test_management_failed_close_keeps_position_and_halts():
    pos = _pos()
    sent = []
    state = BotState(open_positions=[pos])
    d = _deps(mark_position=lambda p: 10.0, close_spread=lambda p, a: "timeout",
              alert_sink=lambda alerts: sent.append(alerts))
    state, results = run_management_cycle(state, d, today="2026-06-17")
    assert results[0].failed is True
    assert state.open_positions == [pos]              # NOT removed (close didn't fill)
    assert state.halted is True and "close" in state.halt_reason
    assert sent  # alerted


def test_management_hold_keeps_position():
    pos = _pos()
    state = BotState(open_positions=[pos])
    d = _deps(mark_position=lambda p: 3.0, dte_of=lambda p, today: 5)  # midrange, high dte
    state, results = run_management_cycle(state, d, today="2026-06-17")
    assert results == [] and state.open_positions == [pos]


def _chain():
    return [OptionQuote(572.0, 0.45, 4.50, 4.60), OptionQuote(568.0, 0.36, 3.40, 3.50),
            OptionQuote(565.0, 0.30, 2.80, 2.90), OptionQuote(560.0, 0.22, 2.00, 2.10),
            OptionQuote(558.0, 0.18, 1.60, 1.70)]


def _acct(today, conc):
    return AccountState(20_000.0, 20_000.0, 0.0, conc, 0.0, {}, today)


def test_entry_opens_position_on_monday_after_10():
    state = BotState()
    d = _deps(get_chain=lambda sym, exp: _chain(), account_state=_acct,
              open_spread=lambda payload: "filled")
    now = datetime(2026, 6, 15, 10, 5)   # Monday 10:05
    state, info = run_entry_cycle(state, d, now)
    assert len(state.open_positions) == 1
    assert state.open_positions[0].short_strike == 568.0


def test_no_entry_on_tuesday():
    state = BotState()
    d = _deps(get_chain=lambda sym, exp: _chain(), account_state=_acct)
    state, info = run_entry_cycle(state, d, datetime(2026, 6, 16, 10, 5))  # Tuesday
    assert state.open_positions == []


def test_no_entry_before_10():
    state = BotState()
    d = _deps(get_chain=lambda sym, exp: _chain(), account_state=_acct)
    state, info = run_entry_cycle(state, d, datetime(2026, 6, 15, 9, 45))  # Mon 09:45
    assert state.open_positions == []


def test_no_entry_when_halted():
    state = BotState(halted=True, halt_reason="x")
    d = _deps(get_chain=lambda sym, exp: _chain(), account_state=_acct)
    state, info = run_entry_cycle(state, d, datetime(2026, 6, 15, 10, 5))
    assert state.open_positions == []


def test_no_entry_when_already_holding():
    state = BotState(open_positions=[_pos()])
    d = _deps(get_chain=lambda sym, exp: _chain(), account_state=_acct)
    state, info = run_entry_cycle(state, d, datetime(2026, 6, 15, 10, 5))
    assert len(state.open_positions) == 1   # unchanged, no second entry


def test_tick_enters_on_clean_monday():
    state = BotState()
    d = _deps(get_chain=lambda sym, exp: _chain(), account_state=_acct,
              broker_positions=lambda: [], open_spread=lambda payload: "filled")
    state = tick(state, d, datetime(2026, 6, 15, 10, 5))   # Monday, flat, clean reconcile
    assert len(state.open_positions) == 1


def test_tick_reconcile_halt_blocks_entry_same_tick():
    # broker shows an untracked position -> reconcile halts -> no entry even though it's Monday 10:05
    state = BotState()
    d = _deps(get_chain=lambda sym, exp: _chain(), account_state=_acct,
              broker_positions=lambda: [_pos()], open_spread=lambda payload: "filled")
    state = tick(state, d, datetime(2026, 6, 15, 10, 5))
    assert state.halted is True and state.open_positions == []


def test_tick_manages_then_holds_no_new_entry_when_holding():
    pos = _pos()
    state = BotState(open_positions=[pos])
    d = _deps(get_chain=lambda sym, exp: _chain(), account_state=_acct,
              broker_positions=lambda: [_pos()], mark_position=lambda p: 3.0,
              dte_of=lambda p, today: 5)            # HOLD
    state = tick(state, d, datetime(2026, 6, 15, 10, 5))
    assert len(state.open_positions) == 1          # still holding the one, no second entry


# ── C1: reconcile error must NOT block management (stops must still fire) ──────

def test_tick_reconcile_error_still_manages():
    """broker_positions raises -> reconcile fails -> management STILL runs -> stop fires."""
    pos = _pos()
    state = BotState(open_positions=[pos])
    d = _deps(
        broker_positions=lambda: (_ for _ in ()).throw(ConnectionError("feed down")),
        mark_position=lambda p: 10.0,        # past stop threshold
        close_spread=lambda p, a: "filled",
        alert_sink=lambda alerts: None,
    )
    state = tick(state, d, datetime(2026, 6, 15, 10, 5))
    assert state.open_positions == []        # management ran, position was closed
    assert state.halted is False             # reconcile error is transient, not a halt


# ── C2: entry must size on live account equity, not the stale Deps constant ────

def test_entry_sizes_on_live_account_equity():
    """account_state returns 10k equity; contracts_for_risk(10_000,830,0.10)=1; gate allows."""
    today = "2026-06-15"
    conc = 0

    def _acct_10k(t, c):
        return AccountState(10_000.0, 10_000.0, 0.0, c, 0.0, {}, t)

    state = BotState()
    d = _deps(get_chain=lambda sym, exp: _chain(), account_state=_acct_10k,
              open_spread=lambda payload: "filled")
    state, _ = run_entry_cycle(state, d, datetime(2026, 6, 15, 10, 5))
    assert len(state.open_positions) == 1
    assert state.open_positions[0].qty == 1


# ── I1: clear_halt resets halted state ─────────────────────────────────────────

def test_clear_halt_resets():
    s = BotState(halted=True, halt_reason="reconcile drift")
    s.clear_halt()
    assert s.halted is False and s.halt_reason == ""


# ── I2: no entry after regular trading hours (≥16:00 ET) ───────────────────────

def test_no_entry_after_hours():
    state = BotState()
    d = _deps(get_chain=lambda sym, exp: _chain(), account_state=_acct)
    state, info = run_entry_cycle(state, d, datetime(2026, 6, 15, 16, 30))  # Monday 16:30
    assert state.open_positions == []


# ── A/B: all-days variant (entry_days, max_open, one-entry-per-day) ─────────────

ALLDAYS = frozenset({0, 1, 2, 3, 4})


def test_alldays_enters_on_tuesday():
    state = BotState()
    d = _deps(get_chain=lambda sym, exp: _chain(), account_state=_acct,
              open_spread=lambda payload: "filled", entry_days=ALLDAYS, max_open=3)
    state, _ = run_entry_cycle(state, d, datetime(2026, 6, 16, 10, 5))   # Tuesday 10:05
    assert len(state.open_positions) == 1
    assert state.last_entry_date == "2026-06-16"


def test_monday_only_bot_skips_tuesday():
    # the default (Bot A) does NOT enter on Tuesday even with a tradeable chain
    state = BotState()
    d = _deps(get_chain=lambda sym, exp: _chain(), account_state=_acct,
              open_spread=lambda payload: "filled")   # default entry_days={0}, max_open=1
    state, _ = run_entry_cycle(state, d, datetime(2026, 6, 16, 10, 5))   # Tuesday
    assert state.open_positions == []


def test_one_entry_per_day_guard():
    state = BotState()
    d = _deps(get_chain=lambda sym, exp: _chain(), account_state=_acct,
              open_spread=lambda payload: "filled", entry_days=ALLDAYS, max_open=3)
    now = datetime(2026, 6, 16, 10, 5)
    state, _ = run_entry_cycle(state, d, now)          # first entry of the day
    state, _ = run_entry_cycle(state, d, now)          # same day again -> blocked
    assert len(state.open_positions) == 1


def test_alldays_allows_concurrent_up_to_max_open():
    state = BotState(open_positions=[_pos(), _pos()])  # already holding 2
    d = _deps(get_chain=lambda sym, exp: _chain(), account_state=_acct,
              open_spread=lambda payload: "filled", entry_days=ALLDAYS, max_open=3)
    state, _ = run_entry_cycle(state, d, datetime(2026, 6, 17, 10, 5))   # Wednesday
    assert len(state.open_positions) == 3              # entered a 3rd (under the cap)


def test_shared_account_run_reconcile_ignores_untracked():
    # two bots on one account: this bot is flat, broker shows the OTHER bot's position.
    # In shared mode it must NOT halt on that untracked position.
    state = BotState(open_positions=[])
    d = _deps(broker_positions=lambda: [_pos()], shared_account=True)
    state, _ = run_reconcile_cycle(state, d)
    assert state.halted is False


def test_strict_account_still_halts_on_untracked():
    # control: default (strict) mode DOES halt on an untracked broker position
    state = BotState(open_positions=[])
    d = _deps(broker_positions=lambda: [_pos()])      # shared_account defaults False
    state, _ = run_reconcile_cycle(state, d)
    assert state.halted is True


# ── per-bot trade logging (for measuring the A/B on a shared account) ───────────

def test_entry_logs_open_trade():
    recs = []
    state = BotState()
    d = _deps(get_chain=lambda sym, exp: _chain(), account_state=_acct,
              open_spread=lambda payload: "filled", trade_log=lambda r: recs.append(r))
    run_entry_cycle(state, d, datetime(2026, 6, 15, 10, 5))   # Monday
    opens = [r for r in recs if r["event"] == "OPEN"]
    assert opens and opens[0]["short"] == 568.0 and opens[0]["status"] == "filled"


def test_management_logs_close_with_realized_pnl():
    recs = []
    pos = ManagedPosition("SPY", 568.0, 558.0, credit=3.0, qty=2, expiry="2026-06-19")
    state = BotState(open_positions=[pos])
    # exit mark 1.5 = TP level (credit*0.5); pnl = (3.0 - 1.5) * 100 * 2 = 300
    d = _deps(mark_position=lambda p: 1.5, dte_of=lambda p, today: 5,
              close_spread=lambda p, a: "filled", trade_log=lambda r: recs.append(r))
    run_management_cycle(state, d, today="2026-06-19")
    closes = [r for r in recs if r["event"] == "CLOSE"]
    assert closes and closes[0]["action"] == "take_profit" and closes[0]["pnl"] == 300.0


# ── per-day entry cap (max_entries_per_day) ────────────────────────────────────

def test_three_entries_per_day_cap():
    state = BotState()
    d = _deps(get_chain=lambda sym, exp: _chain(), account_state=_acct,
              open_spread=lambda payload: "filled",
              entry_days=ALLDAYS, max_open=9, max_entries_per_day=3)
    now = datetime(2026, 6, 16, 10, 5)        # Tuesday; repeated same-day ticks
    for _ in range(6):
        run_entry_cycle(state, d, now)
    assert state.entries_today == 3            # capped at 3 entries this day
    assert len(state.open_positions) == 3


def test_entries_today_resets_next_day():
    state = BotState(last_entry_date="2026-06-16", entries_today=3, open_positions=[])
    d = _deps(get_chain=lambda sym, exp: _chain(), account_state=_acct,
              open_spread=lambda payload: "filled",
              entry_days=ALLDAYS, max_open=9, max_entries_per_day=3)
    run_entry_cycle(state, d, datetime(2026, 6, 17, 10, 5))   # next day -> counter resets, can enter
    assert state.entries_today == 1 and len(state.open_positions) == 1
