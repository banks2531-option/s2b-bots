"""Flow-flip de-gross: on a market-wide bull->bear options-flow flip, close any SAME-DAY position
that is NOT yet profitable (debit-to-close mark >= entry credit). Profitable or prior-day positions
are left to normal management. Opt-in (deps.degross_on_flow_flip); default OFF -> back-compat."""
from datetime import datetime

from bot.app.orchestrator import BotState, Deps, run_flow_degross_cycle, tick
from bot.strategy.manage import ManagedPosition, ExitAction
from bot.regime.state import RegimeState

TODAY = "2026-06-17"


def _pos(entry_date=TODAY, short=568.0, long=558.0, credit=3.0, qty=1, expiry="2026-06-19"):
    return ManagedPosition("SPY", short, long, credit, qty, expiry, entry_date=entry_date)


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


def _flip_state(prev="bullish", **kw):
    return BotState(prev_flow_bias=prev, **kw)


# (a) bull->bear flip closes a same-day, not-yet-profitable position (mark >= credit) ──────────────
def test_flip_closes_same_day_unprofitable_position():
    state = _flip_state(open_positions=[_pos()])
    d = _deps(mark_position=lambda p: 3.5, close_spread=lambda p, a: "filled",  # mark 3.5 >= credit 3.0
              degross_on_flow_flip=True)
    state, closed = run_flow_degross_cycle(state, d, today=TODAY,
                                           regime=RegimeState(flow_bias="bearish"))
    assert len(closed) == 1 and state.open_positions == []


def test_flip_closes_at_breakeven_mark_equals_credit():
    # boundary: mark == credit counts as "not yet profitable" (>= credit) -> closed
    state = _flip_state(open_positions=[_pos()])
    d = _deps(mark_position=lambda p: 3.0, close_spread=lambda p, a: "filled",
              degross_on_flow_flip=True)
    state, closed = run_flow_degross_cycle(state, d, today=TODAY,
                                           regime=RegimeState(flow_bias="bearish"))
    assert len(closed) == 1 and state.open_positions == []


def test_flip_uses_flow_degross_exit_action():
    seen = []
    state = _flip_state(open_positions=[_pos()])
    d = _deps(mark_position=lambda p: 3.5,
              close_spread=lambda p, a: (seen.append(a) or "filled"),
              degross_on_flow_flip=True)
    run_flow_degross_cycle(state, d, today=TODAY, regime=RegimeState(flow_bias="bearish"))
    assert seen == [ExitAction.FLOW_DEGROSS]


# (b) does NOT close a same-day position that is already profitable (mark < credit) ────────────────
def test_flip_keeps_profitable_same_day_position():
    state = _flip_state(open_positions=[_pos()])
    d = _deps(mark_position=lambda p: 2.5, close_spread=lambda p, a: "filled",  # mark 2.5 < credit 3.0
              degross_on_flow_flip=True)
    state, closed = run_flow_degross_cycle(state, d, today=TODAY,
                                           regime=RegimeState(flow_bias="bearish"))
    assert closed == [] and len(state.open_positions) == 1   # profitable -> left to normal management


# (c) does NOT close a position opened on a PRIOR day (entry_date != today) ────────────────────────
def test_flip_keeps_prior_day_position_even_if_unprofitable():
    state = _flip_state(open_positions=[_pos(entry_date="2026-06-16")])
    d = _deps(mark_position=lambda p: 5.0, close_spread=lambda p, a: "filled",  # deep unprofitable
              degross_on_flow_flip=True)
    state, closed = run_flow_degross_cycle(state, d, today=TODAY,
                                           regime=RegimeState(flow_bias="bearish"))
    assert closed == [] and len(state.open_positions) == 1   # prior-day -> not recent -> left alone


# (d) no flip -> no action ─────────────────────────────────────────────────────────────────────────
def test_no_action_when_bearish_to_bearish():
    state = _flip_state(prev="bearish", open_positions=[_pos()])
    d = _deps(mark_position=lambda p: 5.0, close_spread=lambda p, a: "filled",
              degross_on_flow_flip=True)
    state, closed = run_flow_degross_cycle(state, d, today=TODAY,
                                           regime=RegimeState(flow_bias="bearish"))
    assert closed == [] and len(state.open_positions) == 1   # no flip (stayed bearish)


def test_no_action_when_flip_target_is_neutral():
    state = _flip_state(prev="bullish", open_positions=[_pos()])
    d = _deps(mark_position=lambda p: 5.0, degross_on_flow_flip=True)
    state, closed = run_flow_degross_cycle(state, d, today=TODAY,
                                           regime=RegimeState(flow_bias="neutral"))
    assert closed == [] and len(state.open_positions) == 1   # bullish->neutral is not a bull->bear flip


def test_no_action_when_no_prior_bias():
    # first tick (prev_flow_bias == "") landing on bearish is NOT a flip from bullish
    state = _flip_state(prev="", open_positions=[_pos()])
    d = _deps(mark_position=lambda p: 5.0, degross_on_flow_flip=True)
    state, closed = run_flow_degross_cycle(state, d, today=TODAY,
                                           regime=RegimeState(flow_bias="bearish"))
    assert closed == [] and len(state.open_positions) == 1


def test_no_action_when_regime_none():
    # fail-safe: a regime FAULT (None) must never trigger liquidation
    state = _flip_state(open_positions=[_pos()])
    d = _deps(mark_position=lambda p: 5.0, degross_on_flow_flip=True)
    state, closed = run_flow_degross_cycle(state, d, today=TODAY, regime=None)
    assert closed == [] and len(state.open_positions) == 1


# (e) flag OFF -> no action even on a flip (back-compat, default OFF) ──────────────────────────────
def test_no_action_when_flag_disabled():
    state = _flip_state(open_positions=[_pos()])
    d = _deps(mark_position=lambda p: 5.0, close_spread=lambda p, a: "filled",
              degross_on_flow_flip=False)   # default OFF
    state, closed = run_flow_degross_cycle(state, d, today=TODAY,
                                           regime=RegimeState(flow_bias="bearish"))
    assert closed == [] and len(state.open_positions) == 1


# ── fail-safe / logging parity with run_degross_cycle ─────────────────────────────────────────────
def test_flow_degross_failed_close_keeps_position_and_does_not_halt():
    sent = []
    state = _flip_state(open_positions=[_pos()])
    d = _deps(mark_position=lambda p: 3.5, close_spread=lambda p, a: "timeout",
              degross_on_flow_flip=True, alert_sink=lambda alerts: sent.append(alerts))
    state, closed = run_flow_degross_cycle(state, d, today=TODAY,
                                           regime=RegimeState(flow_bias="bearish"))
    assert closed == [] and len(state.open_positions) == 1   # not removed (close didn't fill)
    assert state.halted is False                              # never halts
    assert sent                                              # but alerted


def test_flow_degross_logs_close_with_realized_pnl():
    recs = []
    pos = _pos(credit=3.0, qty=2)
    state = _flip_state(open_positions=[pos])
    # exit mark 3.5; pnl = (3.0 - 3.5) * 100 * 2 = -100 (closing a losing same-day trade to cut risk)
    d = _deps(mark_position=lambda p: 3.5, close_spread=lambda p, a: "filled",
              degross_on_flow_flip=True, trade_log=lambda r: recs.append(r))
    run_flow_degross_cycle(state, d, today=TODAY, regime=RegimeState(flow_bias="bearish"))
    deg = [r for r in recs if r["event"] == "FLOW_DEGROSS"]
    assert deg and deg[0]["action"] == "flow_degross" and deg[0]["pnl"] == -100.0


def test_flow_degross_one_position_error_does_not_block_others():
    p1 = _pos(short=568.0, long=558.0)
    p2 = _pos(short=560.0, long=550.0)
    state = _flip_state(open_positions=[p1, p2])

    def close(p, a):
        if p.short_strike == 568.0:
            raise ConnectionError("feed down")
        return "filled"

    d = _deps(mark_position=lambda p: 3.5, close_spread=close, degross_on_flow_flip=True)
    state, closed = run_flow_degross_cycle(state, d, today=TODAY,
                                           regime=RegimeState(flow_bias="bearish"))
    assert len(closed) == 1 and state.open_positions == [p1]   # p2 closed, p1 (errored) retained


def test_flow_degross_mixed_book_closes_only_recent_unprofitable():
    recent_loss = _pos(short=568.0, long=558.0)                  # same-day, unprofitable -> closed
    recent_win = _pos(short=560.0, long=550.0)                   # same-day, profitable -> kept
    prior = _pos(short=555.0, long=545.0, entry_date="2026-06-16")  # prior-day -> kept
    state = _flip_state(open_positions=[recent_loss, recent_win, prior])

    def mark(p):
        return 2.0 if p.short_strike == 560.0 else 4.0          # 560 leg profitable, rest not

    d = _deps(mark_position=mark, close_spread=lambda p, a: "filled", degross_on_flow_flip=True)
    state, closed = run_flow_degross_cycle(state, d, today=TODAY,
                                           regime=RegimeState(flow_bias="bearish"))
    assert len(closed) == 1 and closed[0].short_strike == 568.0
    assert state.open_positions == [recent_win, prior]


# ── tick() integration: prev_flow_bias tracked across ticks; flip de-grosses in place ─────────────
def test_tick_sets_prev_flow_bias_from_regime():
    state = BotState()
    d = _deps(get_chain=lambda sym, exp: [], broker_positions=lambda: [])
    d.regime_provider = lambda: RegimeState(flow_bias="bullish")
    state = tick(state, d, datetime(2026, 6, 16, 12, 0))   # Tuesday midday (no entry needed here)
    assert state.prev_flow_bias == "bullish"


def test_tick_flip_degrosses_same_day_unprofitable_position():
    # tick1 records bullish; tick2 sees bearish -> flip -> same-day unprofitable position de-grossed
    state = BotState(open_positions=[_pos()])
    d = _deps(get_chain=lambda sym, exp: [], broker_positions=lambda: [_pos()],
              mark_position=lambda p: 3.5, dte_of=lambda p, today: 5,
              close_spread=lambda p, a: "filled", degross_on_flow_flip=True)
    biases = iter(["bullish", "bearish"])
    d.regime_provider = lambda: RegimeState(flow_bias=next(biases))
    # tick 1: bullish, position held (mark 3.5 is not a stop: stop = 3.0*3 = 9.0)
    state = tick(state, d, datetime(2026, 6, 17, 12, 0))
    assert state.prev_flow_bias == "bullish" and len(state.open_positions) == 1
    # tick 2: bearish -> flip -> flow-degross closes the same-day unprofitable position
    state = tick(state, d, datetime(2026, 6, 17, 12, 5))
    assert state.open_positions == []


def test_tick_flag_off_does_not_degross_on_flip():
    state = BotState(open_positions=[_pos()])
    d = _deps(get_chain=lambda sym, exp: [], broker_positions=lambda: [_pos()],
              mark_position=lambda p: 3.5, dte_of=lambda p, today: 5,
              close_spread=lambda p, a: "filled", degross_on_flow_flip=False)  # OFF
    biases = iter(["bullish", "bearish"])
    d.regime_provider = lambda: RegimeState(flow_bias=next(biases))
    state = tick(state, d, datetime(2026, 6, 17, 12, 0))
    state = tick(state, d, datetime(2026, 6, 17, 12, 5))
    assert len(state.open_positions) == 1   # unchanged; flow-degross disabled
