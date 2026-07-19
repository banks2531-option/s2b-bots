"""T9 (spec §16, advisor botbnextsteps Step 2): operational entry states.

Observability only -- T9 must not change a single order decision. Its job is to answer "what is the
bot doing right now?" without spamming an identical line every polling cycle: state is reported on
TRANSITION, not per tick.
"""
import pytest
from datetime import datetime

from bot.app.entry_state import EntryState, classify_entry_state, STATE_PRECEDENCE


def test_enum_values_match_the_specification():
    assert [s.value for s in EntryState] == [
        "active", "no_qualified_credit", "no_risk_capacity", "no_gap_capacity",
        "no_valid_quotes", "no_valid_expiration", "risk_off", "data_failure"]


def test_precedence_order_matches_the_specification():
    assert [s.value for s in STATE_PRECEDENCE] == [
        "data_failure", "risk_off", "no_valid_expiration", "no_valid_quotes",
        "no_qualified_credit", "no_gap_capacity", "no_risk_capacity", "active"]


@pytest.mark.parametrize("reason,expected", [
    ("data_failure", EntryState.DATA_FAILURE),
    ("trend_paused", EntryState.RISK_OFF),
    ("regime_blocked", EntryState.RISK_OFF),
    ("no_expiry", EntryState.NO_VALID_EXPIRATION),
    ("no_order", EntryState.NO_VALID_EXPIRATION),
    ("quote_invalid", EntryState.NO_VALID_QUOTES),
    ("quote_wide", EntryState.NO_VALID_QUOTES),
    ("credit_too_low", EntryState.NO_QUALIFIED_CREDIT),
    ("cost_gate", EntryState.NO_QUALIFIED_CREDIT),
    ("filled", EntryState.ACTIVE),
    ("partial", EntryState.ACTIVE),
])
def test_reasons_map_to_states(reason, expected):
    assert classify_entry_state(reason) is expected


def test_zero_capacity_is_split_by_the_limiting_gate():
    """The distinction the report needs: was the bot stopped by GAP stress or by an aggregate
    budget? They call for completely different responses."""
    assert classify_entry_state("risk_budget", limiting_gate="gap_1_5atr") is EntryState.NO_GAP_CAPACITY
    assert classify_entry_state("risk_budget", limiting_gate="gap_2atr") is EntryState.NO_GAP_CAPACITY
    assert classify_entry_state("risk_budget", limiting_gate="expiry_stop") is EntryState.NO_RISK_CAPACITY
    assert classify_entry_state("risk_budget", limiting_gate="total_structural") is EntryState.NO_RISK_CAPACITY


def test_zero_capacity_without_a_named_gate_defaults_to_risk_capacity():
    assert classify_entry_state("risk_budget", limiting_gate=None) is EntryState.NO_RISK_CAPACITY


def test_unknown_reason_is_treated_as_active_not_a_failure():
    """An unrecognized reason must not masquerade as DATA_FAILURE -- that would turn every new
    decision string into a phantom outage in the report."""
    assert classify_entry_state("some_future_reason") is EntryState.ACTIVE


def test_none_reason_is_active():
    assert classify_entry_state(None) is EntryState.ACTIVE


# ── transition-only reporting ───────────────────────────────────────────────────────────────────

from bot.app.entry_state import note_entry_state

NOW = datetime(2026, 7, 20, 10, 5)
LATER = datetime(2026, 7, 20, 10, 35)


class _S:
    def __init__(self):
        self.current_entry_state = None
        self.entry_state_changed_at = None


def test_first_observation_is_a_transition():
    s, logged = _S(), []
    assert note_entry_state(s, EntryState.ACTIVE, NOW, logged.append) is True
    assert len(logged) == 1
    assert logged[0]["previous_state"] is None
    assert logged[0]["new_state"] == "active"
    assert s.current_entry_state == "active"
    assert s.entry_state_changed_at == NOW.isoformat()


def test_repeated_identical_state_is_not_logged_again():
    """The whole point of §16: a bot sitting in one state for six hours must not write 360 identical
    lines. Without this the state log is noise and nobody reads it."""
    s, logged = _S(), []
    for _ in range(50):
        note_entry_state(s, EntryState.NO_RISK_CAPACITY, NOW, logged.append)
    assert len(logged) == 1


def test_a_real_change_logs_once_with_both_sides():
    s, logged = _S(), []
    note_entry_state(s, EntryState.ACTIVE, NOW, logged.append)
    note_entry_state(s, EntryState.NO_GAP_CAPACITY, LATER, logged.append)
    assert len(logged) == 2
    assert logged[1]["previous_state"] == "active"
    assert logged[1]["new_state"] == "no_gap_capacity"
    assert s.entry_state_changed_at == LATER.isoformat()


def test_returning_to_a_previous_state_logs_again():
    s, logged = _S(), []
    note_entry_state(s, EntryState.ACTIVE, NOW, logged.append)
    note_entry_state(s, EntryState.NO_RISK_CAPACITY, NOW, logged.append)
    note_entry_state(s, EntryState.ACTIVE, LATER, logged.append)
    assert [r["new_state"] for r in logged] == ["active", "no_risk_capacity", "active"]


def test_context_is_carried_onto_the_transition_record():
    s, logged = _S(), []
    note_entry_state(s, EntryState.NO_GAP_CAPACITY, NOW, logged.append,
                     context={"limiting_gate": "gap_1_5atr", "unique_candidates": 4})
    assert logged[0]["limiting_gate"] == "gap_1_5atr"
    assert logged[0]["unique_candidates"] == 4


def test_no_sink_is_harmless():
    s = _S()
    assert note_entry_state(s, EntryState.ACTIVE, NOW, None) is True
    assert s.current_entry_state == "active"


# ── wired into run_entry_cycle (observability only) ─────────────────────────────────────────────

from bot.app.orchestrator import BotState, Deps, run_entry_cycle
from bot.strategy.s2b import OptionQuote
from bot.risk_gate import AccountState
from bot.features import S2bFeatures


def _chain(short_bid=4.00, long_ask=2.00):
    return [OptionQuote(568.0, 0.36, short_bid, short_bid + 0.10),
            OptionQuote(558.0, 0.18, long_ask - 0.10, long_ask)]


def _deps(features, **over):
    base = dict(
        get_spot=lambda sym: 575.0, get_atr=lambda sym: 6.0,
        get_chain=lambda sym, exp: _chain(),
        pick_expiry=lambda today: "2026-07-24",
        get_vix_regime=lambda: (0.5, 0.01),
        account_state=lambda today, conc: AccountState(100_000.0, 100_000.0, 0.0, conc, 0.0, {}, today),
        mark_position=lambda p: 3.0, dte_of=lambda p, today: 4,
        open_spread=lambda payload: "filled", close_spread=lambda p, a: "filled",
        broker_positions=lambda: [], broker_equity=lambda: 100_000.0,
        bot_equity=lambda: 100_000.0, alert_sink=lambda alerts: None,
        risk_equity=lambda: 100_000.0, max_open=9, max_entries_per_day=9, features=features,
    )
    base.update(over)
    return Deps(**base)


MON = datetime(2026, 7, 20, 10, 5)


def test_state_is_recorded_and_persists_across_restart(tmp_path):
    from bot.app.state_store import save_state, load_state
    logged = []
    f = S2bFeatures(aggregate_risk_budget=True, entry_state_tracking=True)
    state, info = run_entry_cycle(BotState(), _deps(f, trade_log=lambda r: logged.append(r)), MON)
    assert info == "filled"
    assert state.current_entry_state == "active"
    transitions = [r for r in logged if r.get("event") == "ENTRY_STATE"]
    assert len(transitions) == 1
    p = str(tmp_path / "s.json")
    save_state(state, p)
    assert load_state(p).current_entry_state == "active"


def test_repeated_cycles_in_the_same_state_emit_one_record():
    logged = []
    f = S2bFeatures(aggregate_risk_budget=True, entry_state_tracking=True)
    d = _deps(f, trade_log=lambda r: logged.append(r))
    state = BotState()
    for minute in (5, 20, 35, 50):
        state, _ = run_entry_cycle(state, d, datetime(2026, 7, 20, 10, minute))
    assert len([r for r in logged if r.get("event") == "ENTRY_STATE"]) == 1


def test_flag_off_emits_no_state_records():
    logged = []
    f = S2bFeatures(aggregate_risk_budget=True)
    state, _ = run_entry_cycle(BotState(), _deps(f, trade_log=lambda r: logged.append(r)), MON)
    assert not [r for r in logged if r.get("event") == "ENTRY_STATE"]
    assert state.current_entry_state is None


def test_state_tracking_does_not_change_the_order_decision():
    """T9 is observability only: the same inputs must produce the same outcome and quantity with the
    flag on or off."""
    base = dict(aggregate_risk_budget=True)
    off = run_entry_cycle(BotState(), _deps(S2bFeatures(**base)), MON)
    on = run_entry_cycle(BotState(), _deps(S2bFeatures(entry_state_tracking=True, **base)), MON)
    assert off[1] == on[1]
    assert [p.qty for p in off[0].open_positions] == [p.qty for p in on[0].open_positions]
