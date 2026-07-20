"""The unmatched-legs halt must survive reconcile (branch review of ce868b0..9d3380f, finding 1).

An unmatched-leg fill means the broker holds an uncovered SHORT put we never booked: three shorts
against two longs is two covered spreads plus one NAKED short. `run_entry_cycle` halts with reason
"unmatched legs", deliberately outside the ("failed close", "reconcile drift") set that
`run_reconcile_cycle` auto-clears, because no later clean reconcile resolves a naked short -- only
an operator can.

That guarantee did NOT hold. `run_reconcile_cycle` overwrote `halt_reason` unconditionally, and the
triggering condition makes the overwrite certain: an uncovered broker leg IS reconcile drift
(untracked_at_broker / qty_mismatch). So "unmatched legs" became "reconcile drift" on the very next
tick, and the next clean reconcile then auto-cleared it -- resuming entries against a naked short
that no human had ever acknowledged.

These tests pin the ownership rule: reconcile may set and clear ONLY the reasons it owns.
"""
from bot.app.orchestrator import BotState, Deps, run_reconcile_cycle
from bot.strategy.manage import ManagedPosition


def _pos(short=568.0, long_=558.0, qty=1):
    return ManagedPosition("SPY", short, long_, credit=3.0, qty=qty, expiry="2026-06-19")


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


def test_reconcile_drift_does_not_relabel_an_unmatched_legs_halt():
    """The overwrite that made the halt clearable. Reconcile still ALERTS on the drift -- it just
    must not take ownership of a halt reason it cannot resolve."""
    state = BotState(open_positions=[_pos(qty=2)], halted=True, halt_reason="unmatched legs")
    d = _deps(broker_positions=lambda: [_pos(qty=3)])      # qty_mismatch -> other_drift
    state, _ = run_reconcile_cycle(state, d)
    assert state.halted is True
    assert state.halt_reason == "unmatched legs"


def test_clean_reconcile_does_not_clear_an_unmatched_legs_halt():
    """The end of the failure chain: once relabelled, a clean reconcile resumed entries. Even
    with the broker looking tidy, a recorded naked short stays halted until an operator acts."""
    state = BotState(open_positions=[_pos()], halted=True, halt_reason="unmatched legs")
    d = _deps(broker_positions=lambda: [_pos()])           # clean
    state, _ = run_reconcile_cycle(state, d)
    assert state.halted is True
    assert state.halt_reason == "unmatched legs"


def test_reconciled_away_position_does_not_clear_an_unmatched_legs_halt():
    """The other auto-clear path (orchestrator.py:220). A position debouncing away as
    missing-at-broker resolves a phantom close; it says nothing about an uncovered leg."""
    state = BotState(open_positions=[_pos()], halted=True, halt_reason="unmatched legs")
    d = _deps(broker_positions=lambda: [])
    state, _ = run_reconcile_cycle(state, d)               # streak 1
    state, _ = run_reconcile_cycle(state, d)               # removed
    assert state.open_positions == []
    assert state.halted is True and state.halt_reason == "unmatched legs"


def test_operator_clear_halt_still_releases_an_unmatched_legs_halt():
    """Sticky must not mean permanent -- the documented escape hatch has to work."""
    state = BotState(halted=True, halt_reason="unmatched legs")
    state.clear_halt()
    assert state.halted is False and state.halt_reason == ""


# ── the pre-existing reconcile behaviour these fixes must not disturb ──────────

def test_reconcile_still_sets_its_own_drift_reason_from_unhalted():
    d = _deps(broker_positions=lambda: [_pos(qty=3)])
    state, _ = run_reconcile_cycle(BotState(open_positions=[_pos(qty=2)]), d)
    assert state.halted is True and state.halt_reason == "reconcile drift"


def test_reconcile_still_relabels_and_clears_a_reason_it_owns():
    """'failed close' is reconcile-owned: it is in the auto-clear set. Drift may still take it
    over, exactly as before -- narrowing the overwrite must not change this path."""
    state = BotState(open_positions=[_pos(qty=2)], halted=True, halt_reason="failed close")
    state, _ = run_reconcile_cycle(state, _deps(broker_positions=lambda: [_pos(qty=3)]))
    assert state.halt_reason == "reconcile drift"
    state, _ = run_reconcile_cycle(state, _deps(broker_positions=lambda: [_pos(qty=2)]))
    assert state.halted is False and state.halt_reason == ""
