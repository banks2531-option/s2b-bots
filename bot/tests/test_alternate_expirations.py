"""T8 (spec §14, advisor botbnextsteps Step 1): evaluate up to three eligible expirations instead of
repeatedly testing only the nearest.

Without this, the bot can poll the nearest expiration all day, find no capacity, and never notice
that a later expiration would have qualified. The rule that makes it safe: a later expiration is an
ALTERNATIVE, never an EXEMPTION -- it must clear every gate the nearest one would have.
"""
import pytest

from bot.app.expirations import get_candidate_expirations, select_best_candidate, CandidateEvaluation
from bot.portfolio.risk_budget import RiskSizingResult


TODAY = "2026-07-20"        # a Monday


def _avail(*dates):
    return list(dates)


# ── get_candidate_expirations ───────────────────────────────────────────────────────────────────

def test_returns_up_to_three_soonest_eligible_expirations():
    got = get_candidate_expirations(
        _avail("2026-07-24", "2026-07-31", "2026-08-07", "2026-08-14"), TODAY)
    assert [e for e, _ in got] == ["2026-07-24", "2026-07-31", "2026-08-07"]


def test_expirations_inside_minimum_dte_are_excluded():
    """minimum_dte 4: 07-22 is 2 days out and must not be offered, however attractive."""
    got = get_candidate_expirations(_avail("2026-07-21", "2026-07-22", "2026-07-24", "2026-07-31"),
                                    TODAY, minimum_dte=4)
    assert [e for e, _ in got] == ["2026-07-24", "2026-07-31"]


def test_dte_is_reported_with_each_expiration():
    got = get_candidate_expirations(_avail("2026-07-24", "2026-07-31"), TODAY)
    assert got == [("2026-07-24", 4), ("2026-07-31", 11)]


def test_results_are_sorted_soonest_first_regardless_of_input_order():
    got = get_candidate_expirations(_avail("2026-08-07", "2026-07-24", "2026-07-31"), TODAY)
    assert [e for e, _ in got] == ["2026-07-24", "2026-07-31", "2026-08-07"]


def test_duplicates_are_collapsed_so_one_expiry_is_never_evaluated_twice():
    got = get_candidate_expirations(_avail("2026-07-24", "2026-07-24", "2026-07-31"), TODAY)
    assert [e for e, _ in got] == ["2026-07-24", "2026-07-31"]


def test_maximum_count_is_respected():
    got = get_candidate_expirations(
        _avail("2026-07-24", "2026-07-31", "2026-08-07", "2026-08-14"), TODAY, maximum_count=2)
    assert len(got) == 2


def test_no_eligible_expirations_returns_empty():
    assert get_candidate_expirations(_avail("2026-07-21"), TODAY) == []
    assert get_candidate_expirations([], TODAY) == []


def test_unparseable_dates_are_skipped_not_fatal():
    got = get_candidate_expirations(_avail("not-a-date", "2026-07-24"), TODAY)
    assert [e for e, _ in got] == ["2026-07-24"]


# ── select_best_candidate ───────────────────────────────────────────────────────────────────────

def _cand(expiry, dte, profit, ratio, final_qty=1, allowed=True, reason=None):
    return CandidateEvaluation(
        expiry=expiry, dte=dte, short_strike=568.0, long_strike=558.0, wing_width=10.0,
        expected_credit=1.2, credit_ratio=ratio, credit_tier="full",
        quote_age_seconds=1.0, target_to_cost_ratio=5.0,
        requested_qty=3, quality_adjusted_qty=3, final_qty=final_qty,
        expected_net_target_profit_per_contract=(profit / final_qty if final_qty else 0.0),
        expected_net_target_profit=profit,
        risk_result=RiskSizingResult(requested_qty=3, final_qty=final_qty, allowed=allowed,
                                      limiting_gate=None, limiting_quantity=None, caps=[],
                                      reason="ok"),
        allowed=allowed, rejection_reason=reason)


def test_nearest_expiration_wins_when_it_is_best():
    near = _cand("2026-07-24", 4, profit=90.0, ratio=0.13)
    far = _cand("2026-07-31", 11, profit=45.0, ratio=0.16)
    assert select_best_candidate([near, far]).expiry == "2026-07-24"


def test_later_expiration_selected_when_nearest_has_zero_capacity():
    near = _cand("2026-07-24", 4, profit=0.0, ratio=0.13, final_qty=0, allowed=False,
                 reason="risk_budget:expiry_stop")
    far = _cand("2026-07-31", 11, profit=45.0, ratio=0.12)
    assert select_best_candidate([near, far]).expiry == "2026-07-31"


def test_selection_uses_net_target_profit_not_gross_credit():
    """A later expiry with a FATTER credit but lower net profit after costs must lose. Selecting on
    gross credit is how a bot talks itself into an expensive trade."""
    near = _cand("2026-07-24", 4, profit=80.0, ratio=0.11)
    far = _cand("2026-07-31", 11, profit=50.0, ratio=0.19)     # richer ratio, worse net profit
    assert select_best_candidate([near, far]).expiry == "2026-07-24"


def test_one_contract_can_beat_two_when_net_profit_is_higher():
    one = _cand("2026-07-24", 4, profit=90.0, ratio=0.13, final_qty=1)
    two = _cand("2026-07-31", 11, profit=60.0, ratio=0.13, final_qty=2)
    assert select_best_candidate([one, two]).expiry == "2026-07-24"


def test_candidates_with_zero_quantity_are_never_selected():
    assert select_best_candidate([_cand("2026-07-24", 4, 90.0, 0.13, final_qty=0)]) is None


def test_candidates_with_nonpositive_profit_are_never_selected():
    assert select_best_candidate([_cand("2026-07-24", 4, 0.0, 0.13)]) is None
    assert select_best_candidate([_cand("2026-07-24", 4, -5.0, 0.13)]) is None


def test_disallowed_candidates_are_never_selected():
    assert select_best_candidate(
        [_cand("2026-07-24", 4, 90.0, 0.13, allowed=False, reason="credit_too_low")]) is None


def test_all_blocked_returns_none():
    assert select_best_candidate([
        _cand("2026-07-24", 4, 0.0, 0.13, final_qty=0, allowed=False, reason="risk_budget"),
        _cand("2026-07-31", 11, 0.0, 0.12, final_qty=0, allowed=False, reason="credit_too_low"),
    ]) is None


def test_empty_list_returns_none():
    assert select_best_candidate([]) is None


def test_ties_break_on_credit_ratio_then_shorter_dte():
    a = _cand("2026-07-31", 11, profit=50.0, ratio=0.12)
    b = _cand("2026-08-07", 18, profit=50.0, ratio=0.15)      # same profit, richer ratio -> wins
    assert select_best_candidate([a, b]).expiry == "2026-08-07"
    c = _cand("2026-07-24", 4, profit=50.0, ratio=0.15)       # same profit+ratio, shorter dte -> wins
    assert select_best_candidate([a, b, c]).expiry == "2026-07-24"


def test_selection_is_deterministic_regardless_of_input_order():
    cands = [_cand("2026-07-24", 4, 50.0, 0.15), _cand("2026-07-31", 11, 90.0, 0.12),
             _cand("2026-08-07", 18, 70.0, 0.14)]
    import itertools
    picks = {select_best_candidate(list(p)).expiry for p in itertools.permutations(cands)}
    assert picks == {"2026-07-31"}
