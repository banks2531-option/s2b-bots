"""Advisor directive (2026-07-18), Decisions 1 & 2.

DECISION 1 -- CreditObservation, Option B: build the structured record and emit it to the decision
log for audit, but keep the persisted adaptive history as bare floats. Explicitly NOT a persisted-
state migration: `credit_ratio_history` stays a list of floats and `state` never carries the record.

DECISION 2 -- three separate counters, incremented at three different stages:
    entry_cycles_started         -- every scheduled entry cycle, at the very top
    raw_candidate_evaluations    -- once a valid candidate spread has actually been constructed
    unique_candidate_opportunities -- once dedup says this is a materially new opportunity
The gap between the first two is the advisor's stated goal: reveal how much of the session the bot
spends blocked by max-positions / max-entries before it ever builds a candidate.
"""
from datetime import datetime

from bot.app.orchestrator import BotState, Deps, run_entry_cycle
from bot.strategy.credit_quality import CreditObservation, candidate_key
from bot.strategy.s2b import OptionQuote
from bot.risk_gate import AccountState
from bot.features import S2bFeatures


def _chain():
    return [OptionQuote(568.0, 0.36, 4.00, 4.10), OptionQuote(558.0, 0.18, 1.90, 2.00)]


def _deps(**over):
    base = dict(
        get_spot=lambda sym: 575.0, get_atr=lambda sym: 6.0,
        get_chain=lambda sym, exp: _chain(),
        pick_expiry=lambda today: "2026-06-19",
        get_vix_regime=lambda: (0.5, 0.01),
        account_state=lambda today, conc: AccountState(100_000.0, 100_000.0, 0.0, conc, 0.0, {}, today),
        mark_position=lambda p: 3.0, dte_of=lambda p, today: 5,
        open_spread=lambda payload: "filled", close_spread=lambda p, a: "filled",
        broker_positions=lambda: [], broker_equity=lambda: 100_000.0,
        bot_equity=lambda: 100_000.0, alert_sink=lambda alerts: None,
        risk_equity=lambda: 100_000.0,
    )
    base.update(over)
    return Deps(**base)


MONDAY = datetime(2026, 6, 15, 10, 5)


# ── Decision 1: the record exists, is frozen, and carries the spec's fields ──────────────────────

def test_credit_observation_is_frozen_with_the_specified_fields():
    obs = CreditObservation(timestamp=MONDAY, expiry="2026-06-19", dte_bucket="4-5",
                            short_strike=568.0, long_strike=558.0,
                            expected_executable_credit=2.0, credit_ratio=0.20,
                            candidate_key=candidate_key("2026-06-15", "2026-06-19", 568.0, 558.0, MONDAY))
    assert obs.expiry == "2026-06-19"
    assert obs.dte_bucket == "4-5"
    assert obs.credit_ratio == 0.20
    import dataclasses
    assert dataclasses.is_dataclass(obs)
    try:
        obs.credit_ratio = 0.30            # frozen -> must raise
        raise AssertionError("CreditObservation must be frozen")
    except dataclasses.FrozenInstanceError:
        pass


def test_observation_is_emitted_to_the_log_when_the_dedup_rule_qualifies():
    logged = []
    d = _deps(features=S2bFeatures(credit_tiers=True, decision_logging=True),
              trade_log=lambda r: logged.append(r))
    state, _ = run_entry_cycle(BotState(), d, MONDAY)
    obs = [r for r in logged if r.get("event") == "CREDIT_OBS"]
    assert len(obs) == 1
    rec = obs[0]
    assert rec["expiry"] == "2026-06-19"
    assert rec["obs_dte_bucket"] == "4-5"
    assert rec["short"] == 568.0
    assert rec["long"] == 558.0
    # 0.207, not 0.20: the observation records the CONSERVATIVE executable credit
    # (mid 2.10 less entry slippage = 2.07), which is what the tier decision uses.
    assert rec["credit_ratio"] == 0.207
    assert rec["obs_expected_executable_credit"] == 2.07
    assert rec["obs_candidate_key"] == "2026-06-15|2026-06-19|568.0|558.0|10-0"


def test_no_observation_emitted_when_the_candidate_is_a_duplicate():
    """Same spread, same 15-minute window -> the dedup rule declines, so neither the rolling history
    NOR the audit log gets a second entry. The log must not become a per-poll firehose."""
    logged = []
    d = _deps(features=S2bFeatures(credit_tiers=True, decision_logging=True),
              trade_log=lambda r: logged.append(r), max_open=9, max_entries_per_day=9)
    state = BotState()
    for minute in (1, 8, 14):
        state, _ = run_entry_cycle(state, d, datetime(2026, 6, 15, 10, minute))
    assert len([r for r in logged if r.get("event") == "CREDIT_OBS"]) == 1


def test_persisted_history_stays_floats_and_state_never_holds_the_record():
    """Decision 1's whole point: audit trail WITHOUT a persisted-state migration."""
    d = _deps(features=S2bFeatures(credit_tiers=True), trade_log=lambda r: None)
    state, _ = run_entry_cycle(BotState(), d, MONDAY)
    hist = state.credit_ratio_history["4-5"]
    assert hist == [0.207]
    assert all(isinstance(x, float) for x in hist)
    assert not hasattr(state, "credit_observations")


def test_observation_survives_the_csv_writer(tmp_path):
    """The audit record is worthless if extrasaction='ignore' eats its columns on the way to disk."""
    import csv as _csv
    from bot.app.wiring import make_trade_logger
    p = tmp_path / "trades.csv"
    d = _deps(features=S2bFeatures(credit_tiers=True, decision_logging=True),
              trade_log=make_trade_logger(str(p), include_decision_columns=True))
    run_entry_cycle(BotState(), d, MONDAY)
    rows = [r for r in _csv.DictReader(open(str(p))) if r.get("event") == "CREDIT_OBS"]
    assert len(rows) == 1
    assert rows[0]["obs_dte_bucket"] == "4-5"
    assert rows[0]["obs_candidate_key"] == "2026-06-15|2026-06-19|568.0|558.0|10-0"
    assert rows[0]["obs_expected_executable_credit"] == "2.07"


# ── Decision 2: three counters at three stages ───────────────────────────────────────────────────

def test_all_three_counters_advance_on_a_normal_evaluation():
    d = _deps(features=S2bFeatures())
    state, _ = run_entry_cycle(BotState(), d, MONDAY)
    assert state.entry_cycles_started == 1
    assert state.raw_candidate_evaluations == 1
    assert state.unique_candidate_opportunities == 1


def test_cycles_advance_but_evaluations_do_not_when_blocked_before_a_candidate_exists():
    """The advisor's stated reason for splitting these: a bot sitting at its position limit still
    RUNS cycles, but never constructs a spread. Cycles climb; evaluations stay flat. Without the
    split this looks identical to a bot that evaluated candidates and rejected them all."""
    d = _deps(features=S2bFeatures(), max_open=1, max_entries_per_day=1)
    state = BotState()
    for minute in (5, 20, 35):
        state, info = run_entry_cycle(state, d, datetime(2026, 6, 15, 10, minute))
    assert state.entry_cycles_started == 3
    assert state.raw_candidate_evaluations == 1     # only the first cycle got as far as a spread
    assert state.unique_candidate_opportunities == 1
    # run_entry_cycle returns info=None for the pre-candidate guards (max_open included);
    # the reason lives in the decision log, not the return value.
    assert info is None


def test_evaluations_exceed_unique_opportunities_when_the_same_spread_is_re_polled():
    d = _deps(features=S2bFeatures(), max_open=9, max_entries_per_day=9)
    state = BotState()
    for minute in (1, 8, 14):
        state, _ = run_entry_cycle(state, d, datetime(2026, 6, 15, 10, minute))
    assert state.entry_cycles_started == 3
    assert state.raw_candidate_evaluations == 3
    assert state.unique_candidate_opportunities == 1


def test_counters_round_trip_through_the_state_store(tmp_path):
    from bot.app.state_store import save_state, load_state
    d = _deps(features=S2bFeatures())
    state, _ = run_entry_cycle(BotState(), d, MONDAY)
    p = str(tmp_path / "s.json")
    save_state(state, p)
    r = load_state(p)
    assert (r.entry_cycles_started, r.raw_candidate_evaluations,
            r.unique_candidate_opportunities) == (1, 1, 1)
