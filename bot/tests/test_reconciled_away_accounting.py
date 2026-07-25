"""A reconciled-away position must book its P&L (live finding, 2026-07-20).

WHAT HAPPENED. Bot B held 743/733 x8 in a --shared-account sandbox. A co-occupant closed it --
together with their own foreign 736/731 x68 -- in one 4-leg order Bot B never placed. Bot B saw the
position missing at the broker, debounced, and reconciled it away:

    [ALERT] critical: reconcile drift: equity=0.0 missing=1   (x10)
    [ALERT] warn: reconciled-away (closed at broker) SPY 743.0/733.0

and booked NOTHING. `realized_today` stayed 0.0 while the real close cost 2.77 against a 1.39
credit on 8 contracts -- about -$1,104 that simply left the record.

WHY IT MATTERS BEYOND BOOKKEEPING. `realized_today` feeds the daily-loss halt. A loss that is
never booked cannot trip it, so the bot keeps sizing new trades as though the day were flat. The
silent direction is the dangerous one.

WHY AN ESTIMATE IS THE RIGHT ANSWER. We never see the fill -- someone else placed the order -- so
the exact exit price is unknowable. The last mark is the best available estimate, and booking a
good estimate is strictly better than booking zero, which we know is wrong. When even the mark is
unavailable -- often the case, since a data outage is *why* the position looked missing -- we
refuse to guess and escalate instead.
"""
from bot.app.orchestrator import BotState, Deps, run_reconcile_cycle
from bot.strategy.manage import ManagedPosition
from bot.features import S2bFeatures

TODAY = "2026-07-20"


def _pos(short=743.0, long_=733.0, qty=8, credit=1.39, opening_fees=0.0):
    return ManagedPosition("SPY", short, long_, credit=credit, qty=qty, expiry="2026-07-24",
                           entry_date="2026-07-16", opening_fees=opening_fees)


def _deps(**over):
    base = dict(
        get_spot=lambda sym: 744.0, get_atr=lambda sym: 8.5,
        get_chain=lambda sym, exp: [], pick_expiry=lambda today: "2026-07-24",
        get_vix_regime=lambda: (0.5, 0.01), account_state=lambda today, conc: None,
        mark_position=lambda p: 2.77, dte_of=lambda p, today: 4,
        open_spread=lambda payload: "filled", close_spread=lambda p, a: "filled",
        broker_positions=lambda: [], broker_equity=lambda: 74_000.0,
        bot_equity=lambda: 74_000.0, alert_sink=lambda alerts: None,
    )
    base.update(over)
    return Deps(**base)


def _remove(state, deps, today=TODAY):
    """Drive the debounce to completion (MISSING_REMOVE_THRESHOLD ticks)."""
    for _ in range(2):
        state, _ = run_reconcile_cycle(state, deps, today)
    return state


# ── the fix ───────────────────────────────────────────────────────────────────

def test_reconciled_away_position_books_its_realized_loss():
    """The live case, to the dollar: credit 1.39, mark 2.77, 8 contracts -> -$1,104."""
    rows = []
    d = _deps(trade_log=rows.append, mark_position=lambda p: 2.77)
    state = _remove(BotState(open_positions=[_pos()]), d)
    assert state.open_positions == []
    assert state.realized_today == -1104.0
    assert state.risk_day == TODAY


def test_reconciled_away_position_writes_a_close_row():
    """It must be visible in the trade log, and distinguishable from a close the bot chose to make
    -- the exit price is an estimate, and a reader has to be able to tell."""
    rows = []
    d = _deps(trade_log=rows.append, mark_position=lambda p: 2.77)
    _remove(BotState(open_positions=[_pos()]), d)
    closes = [r for r in rows if r.get("event") == "CLOSE"]
    assert len(closes) == 1
    assert closes[0]["status"] == "reconciled_away"
    assert closes[0]["pnl"] == -1104.0
    assert closes[0]["qty"] == 8
    assert closes[0]["exit_value"] == 2.77


def test_a_profitable_reconcile_away_books_a_gain():
    """Expiry-worthless is the benign case and must book the full credit as profit, not zero."""
    d = _deps(mark_position=lambda p: 0.0)
    state = _remove(BotState(open_positions=[_pos()]), d)
    assert state.realized_today == 1112.0        # 1.39 * 100 * 8


def test_opening_fees_are_subtracted_when_fill_accounting_is_on():
    d = _deps(features=S2bFeatures(actual_fill_accounting=True), mark_position=lambda p: 2.77)
    state = _remove(BotState(open_positions=[_pos(opening_fees=10.4)]), d)
    assert state.realized_today == -1114.4       # -1104.0 - 10.40


# ── refusing to guess ─────────────────────────────────────────────────────────

def test_unmarkable_position_books_nothing_and_escalates():
    """A data outage is usually WHY the position looked missing, so this path is common. Booking a
    fabricated number into the daily-loss gate would be worse than booking none -- but it must be
    loud, which is exactly what the silent version failed to be."""
    alerts = []

    def _boom(p):
        raise RuntimeError("quote feed down")

    d = _deps(mark_position=_boom, alert_sink=alerts.extend)
    state = _remove(BotState(open_positions=[_pos()]), d)
    assert state.open_positions == []            # still removed -- we do not re-halt on it
    assert state.realized_today == 0.0
    text = " ".join(a.message for a in alerts)
    assert "UNBOOKED" in text.upper()
    assert any(a.severity.name == "CRITICAL" for a in alerts), "a silent drop is the bug"


def test_a_none_mark_is_treated_as_unmarkable():
    alerts = []
    d = _deps(mark_position=lambda p: None, alert_sink=alerts.extend)
    state = _remove(BotState(open_positions=[_pos()]), d)
    assert state.realized_today == 0.0
    assert "UNBOOKED" in " ".join(a.message for a in alerts).upper()


# ── everything that must not change ───────────────────────────────────────────

def test_two_arg_call_keeps_the_previous_behaviour_exactly():
    """Every existing caller passes two arguments. Without a date there is no day to accumulate
    into, so the old alert-only path stands and no CLOSE row is invented."""
    rows = []
    d = _deps(trade_log=rows.append)
    state = BotState(open_positions=[_pos()])
    for _ in range(2):
        state, _ = run_reconcile_cycle(state, d)
    assert state.open_positions == []
    assert state.realized_today == 0.0
    assert [r for r in rows if r.get("event") == "CLOSE"] == []


def test_the_warn_alert_still_fires():
    alerts = []
    d = _deps(alert_sink=alerts.extend)
    _remove(BotState(open_positions=[_pos()]), d)
    assert any("reconciled-away" in a.message for a in alerts)


def test_debounce_still_required_before_any_booking():
    """One transient empty read must not book a loss and drop the position."""
    d = _deps(mark_position=lambda p: 2.77)
    state, _ = run_reconcile_cycle(BotState(open_positions=[_pos()]), d, TODAY)
    assert len(state.open_positions) == 1
    assert state.realized_today == 0.0


# ── P2: reconstruct the ACTUAL broker fill (not just an estimate) ──────────────

def test_reconstructed_broker_fill_is_preferred_over_the_mark_estimate():
    """When the actual closing fill can be reconstructed from broker history, book THAT (the real
    number + an order-id audit trail), not the mark estimate."""
    rows = []
    # reconstruct_exit returns the real fill; mark would give a DIFFERENT (wrong) estimate.
    d = _deps(trade_log=rows.append, mark_position=lambda p: 2.77,
              reconstruct_exit=lambda p, today: {"exit_value": 2.50, "pnl": -888.0,
                                                 "source": "tradier_history", "order_id": "ORD-42"})
    state = _remove(BotState(open_positions=[_pos()]), d)
    close = [r for r in rows if r.get("event") == "CLOSE"][0]
    assert state.realized_today == -888.0          # the reconstructed P&L, not the -1104 mark estimate
    assert close["pnl"] == -888.0 and close["exit_value"] == 2.50
    assert close["recon_source"] == "tradier_history" and close["order_id"] == "ORD-42"
    assert close["status"] == "reconciled_away"    # base status unchanged (back-compat)


def test_reconstruct_works_when_the_mark_is_unavailable():
    """The outage case: live quotes time out (mark raises), but order history still resolves the fill,
    so the position books correctly instead of escalating 'reconcile by hand'."""
    alerts = []
    def _boom(p):
        raise RuntimeError("quote feed down")
    d = _deps(mark_position=_boom, alert_sink=alerts.extend,
              reconstruct_exit=lambda p, today: {"exit_value": 0.10, "pnl": 1032.0,
                                                 "source": "tradier_history", "order_id": "ORD-7"})
    state = _remove(BotState(open_positions=[_pos()]), d)
    assert state.realized_today == 1032.0
    assert "UNBOOKED" not in " ".join(a.message for a in alerts).upper()   # no escalation needed


def test_falls_back_to_mark_when_reconstruct_returns_none():
    """No fill found -> the prior mark-estimate path stands, tagged as an estimate."""
    rows = []
    d = _deps(trade_log=rows.append, mark_position=lambda p: 2.77,
              reconstruct_exit=lambda p, today: None)
    state = _remove(BotState(open_positions=[_pos()]), d)
    close = [r for r in rows if r.get("event") == "CLOSE"][0]
    assert state.realized_today == -1104.0
    assert close["recon_source"] == "mark_estimate" and close["exit_value"] == 2.77


def test_reconstruct_exception_falls_back_safely():
    """A broker-history lookup that raises must not break reconciliation -- fall back to the mark."""
    d = _deps(mark_position=lambda p: 2.77,
              reconstruct_exit=lambda p, today: (_ for _ in ()).throw(RuntimeError("history 500")))
    state = _remove(BotState(open_positions=[_pos()]), d)
    assert state.realized_today == -1104.0         # fell back to the mark estimate, no crash


def test_unwired_reconstruct_is_byte_identical_to_before():
    """Default (reconstruct_exit=None) must reproduce the exact prior behaviour."""
    rows = []
    d = _deps(trade_log=rows.append, mark_position=lambda p: 2.77)   # no reconstruct_exit
    state = _remove(BotState(open_positions=[_pos()]), d)
    close = [r for r in rows if r.get("event") == "CLOSE"][0]
    assert state.realized_today == -1104.0 and close["exit_value"] == 2.77
    assert close["recon_source"] == "mark_estimate"
