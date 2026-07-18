"""Task 6 (post-v2 refinement §12): deduplicate candidate signals for REPORTING.

A bot polling every 60s re-evaluates the same spread dozens of times a day. Counting each poll as a
distinct "opportunity" turns every opportunity statistic into a polling-frequency indicator. §12
splits the counting in two:

    raw_candidate_evaluations  -- every evaluated candidate (diagnostics only)
    unique_candidate_opportunities -- materially distinct candidates

A candidate is NEW when its strike pair changes, its expiration changes, a new 15-minute bucket
begins, or its credit ratio moves by >= CREDIT_RATIO_REEVALUATION_CHANGE (0.005).

Spec: "Do not use raw polling evaluations in profitability reports."
"""
from datetime import datetime

import pytest

from bot.strategy.credit_quality import (candidate_key, is_new_candidate,
                                          CREDIT_RATIO_REEVALUATION_CHANGE)


TS_10_01 = datetime(2026, 7, 20, 10, 1)
TS_10_08 = datetime(2026, 7, 20, 10, 8)     # same (hour, minute//15) bucket as 10:01
TS_10_16 = datetime(2026, 7, 20, 10, 16)    # next 15-min bucket
TS_11_01 = datetime(2026, 7, 20, 11, 1)     # same minute//15 sub-bucket, DIFFERENT hour


# ── candidate_key: the spec's §18 test, verbatim ────────────────────────────────────────────────

def test_same_signal_same_bucket_is_not_counted_twice():
    key1 = candidate_key("2026-07-20", "2026-07-31", 745, 735, TS_10_01)
    key2 = candidate_key("2026-07-20", "2026-07-31", 745, 735, TS_10_08)
    assert key1 == key2


def test_candidate_key_shape_matches_spec():
    assert candidate_key("2026-07-20", "2026-07-31", 745.0, 735.0, TS_10_01) == (
        "2026-07-20", "2026-07-31", 745.0, 735.0, (10, 0))


@pytest.mark.parametrize("field,args", [
    ("trading_date", ("2026-07-21", "2026-07-31", 745, 735, TS_10_01)),
    ("expiry",       ("2026-07-20", "2026-08-07", 745, 735, TS_10_01)),
    ("short_strike", ("2026-07-20", "2026-07-31", 746, 735, TS_10_01)),
    ("long_strike",  ("2026-07-20", "2026-07-31", 745, 736, TS_10_01)),
    ("bucket",       ("2026-07-20", "2026-07-31", 745, 735, TS_10_16)),
])
def test_candidate_key_differs_when_any_component_changes(field, args):
    assert candidate_key(*args) != candidate_key("2026-07-20", "2026-07-31", 745, 735, TS_10_01)


def test_bucket_is_hour_qualified_so_same_sub_bucket_in_a_later_hour_is_distinct():
    """The bucket is (hour, minute//15), NOT minute//15 alone -- 10:01 and 11:01 both sit in
    sub-bucket 0 and must NOT collide (that would silently merge four windows a day)."""
    assert candidate_key("2026-07-20", "2026-07-31", 745, 735, TS_11_01) != \
           candidate_key("2026-07-20", "2026-07-31", 745, 735, TS_10_01)


def test_candidate_key_rounds_strikes_to_three_places():
    assert candidate_key("2026-07-20", "2026-07-31", 745.00001, 735.0, TS_10_01) == \
           candidate_key("2026-07-20", "2026-07-31", 745.0, 735.0, TS_10_01)


# ── is_new_candidate: key novelty OR a material credit-ratio move ────────────────────────────────

def test_first_sighting_of_a_key_is_new():
    assert is_new_candidate(key=("k",), ratio=0.12, seen={}) is True


def test_same_key_same_ratio_is_not_new():
    assert is_new_candidate(key=("k",), ratio=0.12, seen={("k",): 0.12}) is False


def test_same_key_with_material_ratio_move_is_new():
    # exactly at the 0.005 threshold -> new (spec: ">= 0.005")
    assert is_new_candidate(key=("k",), ratio=0.125, seen={("k",): 0.12}) is True
    assert is_new_candidate(key=("k",), ratio=0.115, seen={("k",): 0.12}) is True


def test_same_key_with_immaterial_ratio_move_is_not_new():
    assert is_new_candidate(key=("k",), ratio=0.1249, seen={("k",): 0.12}) is False


def test_reevaluation_threshold_constant_matches_spec():
    assert CREDIT_RATIO_REEVALUATION_CHANGE == 0.005


def test_is_new_candidate_records_the_latest_ratio_as_the_new_anchor():
    """The anchor must ADVANCE on each new sighting, otherwise a slow drift of 0.004 per poll never
    trips the threshold against a stale first-seen anchor... and conversely a candidate that drifts
    away and back would re-count. Anchor advancement is the caller's job via record_candidate."""
    seen = {}
    from bot.strategy.credit_quality import record_candidate
    assert record_candidate(key=("k",), ratio=0.120, seen=seen) is True
    assert record_candidate(key=("k",), ratio=0.124, seen=seen) is False   # +0.004, immaterial
    assert seen[("k",)] == 0.124                                            # anchor still advanced
    assert record_candidate(key=("k",), ratio=0.128, seen=seen) is False    # +0.004 from 0.124
    assert record_candidate(key=("k",), ratio=0.133, seen=seen) is True     # +0.005 from 0.128


# ── day pruning: keys carry a trading_date, so yesterday's keys must not accumulate forever ──────

def test_record_candidate_prunes_keys_from_prior_trading_days():
    from bot.strategy.credit_quality import record_candidate
    seen = {("2026-07-17", "e", 1.0, 2.0, (10, 0)): 0.12}
    record_candidate(key=("2026-07-20", "e", 1.0, 2.0, (10, 0)), ratio=0.12, seen=seen,
                     trading_date="2026-07-20")
    assert list(seen) == [("2026-07-20", "e", 1.0, 2.0, (10, 0))]


# ── orchestrator wiring: counters advance per evaluation, dedup across a 15-min bucket ───────────

from bot.app.orchestrator import BotState, Deps, run_entry_cycle
from bot.strategy.s2b import OptionQuote
from bot.risk_gate import AccountState
from bot.features import S2bFeatures


def _chain():
    return [OptionQuote(568.0, 0.36, 4.00, 4.10), OptionQuote(558.0, 0.18, 1.90, 2.00)]


def _od_deps(**over):
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


def test_repeated_polls_of_the_same_spread_count_once_as_an_opportunity():
    """Three polls inside one 15-min bucket on the identical spread: raw evaluations advance every
    time, unique opportunities exactly once. This is the §12 point -- without it, opportunity counts
    just measure how often the bot polls."""
    state = BotState()
    d = _od_deps(features=S2bFeatures(), max_open=9, max_entries_per_day=9)
    for minute in (1, 8, 14):
        state, _ = run_entry_cycle(state, d, datetime(2026, 6, 15, 10, minute))
    assert state.raw_candidate_evaluations == 3
    assert state.unique_candidate_opportunities == 1


def test_next_15_minute_bucket_counts_a_fresh_opportunity():
    state = BotState()
    d = _od_deps(features=S2bFeatures(), max_open=9, max_entries_per_day=9)
    for minute in (1, 16):
        state, _ = run_entry_cycle(state, d, datetime(2026, 6, 15, 10, minute))
    assert state.raw_candidate_evaluations == 2
    assert state.unique_candidate_opportunities == 2


def test_counters_survive_a_state_store_round_trip_without_recounting(tmp_path):
    """Regression guard for the tuple/list JSON asymmetry: candidate_key builds a TUPLE bucket, JSON
    round-trips it as a LIST. If load_state didn't rebuild the tuple, no persisted key would ever
    compare equal again and every restart would re-count the whole day as new opportunities."""
    from bot.app.state_store import save_state, load_state
    state = BotState()
    d = _od_deps(features=S2bFeatures(), max_open=9, max_entries_per_day=9)
    state, _ = run_entry_cycle(state, d, datetime(2026, 6, 15, 10, 1))
    assert state.unique_candidate_opportunities == 1

    p = str(tmp_path / "state.json")
    save_state(state, p)
    reloaded = load_state(p)
    assert reloaded.seen_candidate_keys == state.seen_candidate_keys      # tuples, not lists
    assert reloaded.raw_candidate_evaluations == 1
    assert reloaded.unique_candidate_opportunities == 1

    # same spread, same bucket, AFTER a restart -> still not a new opportunity
    reloaded, _ = run_entry_cycle(reloaded, d, datetime(2026, 6, 15, 10, 8))
    assert reloaded.raw_candidate_evaluations == 2
    assert reloaded.unique_candidate_opportunities == 1
