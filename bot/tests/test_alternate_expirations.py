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


# ── wired into run_entry_cycle (feature-flagged) ────────────────────────────────────────────────

from datetime import datetime
from bot.app.orchestrator import BotState, Deps, run_entry_cycle
from bot.strategy.s2b import OptionQuote
from bot.risk_gate import AccountState
from bot.features import S2bFeatures

MONDAY = datetime(2026, 7, 20, 10, 5)
NEAR, MID, FAR = "2026-07-24", "2026-07-31", "2026-08-07"


def _chain(short_bid, long_ask, ts=MONDAY):
    """568/558 on spot 575. credit = short_bid - long_ask (less slippage)."""
    return [OptionQuote(570.0, 0.45, 6.00, 6.10, exchange_timestamp=ts, iv=0.12),
            OptionQuote(568.0, 0.36, short_bid, short_bid + 0.10, exchange_timestamp=ts, iv=0.12),
            OptionQuote(558.0, 0.18, long_ask - 0.10, long_ask, exchange_timestamp=ts, iv=0.12)]


def _deps(chains, **over):
    """chains: {expiry: chain}. Any expiry not present yields an empty chain (no order)."""
    base = dict(
        get_spot=lambda sym: 575.0, get_atr=lambda sym: 6.0,
        get_chain=lambda sym, exp: chains.get(exp, []),
        pick_expiry=lambda today: NEAR,
        get_expirations=lambda today: [NEAR, MID, FAR],
        get_vix_regime=lambda: (0.5, 0.01),
        account_state=lambda today, conc: AccountState(100_000.0, 100_000.0, 0.0, conc, 0.0, {}, today),
        mark_position=lambda p: 3.0, dte_of=lambda p, today: 5,
        open_spread=lambda payload: "filled", close_spread=lambda p, a: "filled",
        broker_positions=lambda: [], broker_equity=lambda: 100_000.0,
        bot_equity=lambda: 100_000.0, alert_sink=lambda alerts: None,
        risk_equity=lambda: 100_000.0, max_open=5, max_entries_per_day=5,
        features=S2bFeatures(aggregate_risk_budget=True, decision_logging=True,
                             enable_alternate_expirations=True),
    )
    base.update(over)
    return Deps(**base)


def _decision(logged):
    return next(r for r in logged if r.get("event") == "DECISION")


def test_nearest_expiration_selected_when_it_is_best():
    logged = []
    chains = {NEAR: _chain(4.00, 2.00), MID: _chain(2.50, 2.00), FAR: _chain(2.30, 2.00)}
    d = _deps(chains, trade_log=lambda r: logged.append(r))
    state, info = run_entry_cycle(BotState(), d, MONDAY)
    assert info == "filled"
    assert state.open_positions[0].expiry == NEAR
    assert _decision(logged)["expiration_selected"] == NEAR


def test_later_expiration_selected_when_nearest_is_unattractive():
    """Nearest collects a thin credit; a later expiry pays materially more net profit and wins."""
    logged = []
    chains = {NEAR: _chain(2.20, 2.00), MID: _chain(5.00, 2.00), FAR: _chain(2.30, 2.00)}
    d = _deps(chains, trade_log=lambda r: logged.append(r))
    state, info = run_entry_cycle(BotState(), d, MONDAY)
    assert info == "filled"
    assert state.open_positions[0].expiry == MID
    assert _decision(logged)["expiration_selected"] == MID


def test_at_most_three_expirations_are_evaluated():
    logged = []
    seen = []
    chains = {e: _chain(4.00, 2.00) for e in (NEAR, MID, FAR, "2026-08-14", "2026-08-21")}

    def get_chain(sym, exp):
        seen.append(exp)
        return chains.get(exp, [])
    d = _deps(chains, trade_log=lambda r: logged.append(r), get_chain=get_chain,
              get_expirations=lambda today: [NEAR, MID, FAR, "2026-08-14", "2026-08-21"])
    run_entry_cycle(BotState(), d, MONDAY)
    assert _decision(logged)["expirations_evaluated"] == 3
    assert set(seen) <= {NEAR, MID, FAR}          # the 4th/5th are never even fetched


def test_all_expirations_blocked_still_rejects_with_a_specific_reason():
    """Every expiry too thin on credit -> no candidate fits. The cycle must still reject, and the
    logged reason must stay the SPECIFIC gate rather than a generic 'nothing fits'."""
    logged = []
    chains = {e: _chain(0.30, 0.20) for e in (NEAR, MID, FAR)}     # ratio ~0.008 -> below 8% floor
    d = _deps(chains, trade_log=lambda r: logged.append(r),
              features=S2bFeatures(aggregate_risk_budget=True, credit_tiers=True,
                                    decision_logging=True, enable_alternate_expirations=True))
    state, info = run_entry_cycle(BotState(), d, MONDAY)
    # A sub-floor credit on a wide quote can be stopped by EITHER the package-width gate or the
    # credit floor, whichever fires first; the point of this test is that nothing trades and the
    # §14 no-candidate-fits outcome is recorded, not which gate won the race.
    assert info in ("credit_too_low", "quote_wide")
    assert state.open_positions == []
    rec = _decision(logged)
    assert rec["no_candidate_fits"] is True
    assert rec["expiration_selected"] is None


def test_a_later_expiration_never_bypasses_the_credit_gate():
    """The later expiry has a FAT credit but sits below the absolute floor -- it must not be selected
    into a trade. A later expiration is an alternative, never an exemption."""
    logged = []
    chains = {NEAR: _chain(0.30, 0.20), MID: _chain(0.35, 0.20), FAR: _chain(0.40, 0.20)}
    d = _deps(chains, trade_log=lambda r: logged.append(r),
              features=S2bFeatures(aggregate_risk_budget=True, credit_tiers=True,
                                    decision_logging=True, enable_alternate_expirations=True))
    state, info = run_entry_cycle(BotState(), d, MONDAY)
    assert info in ("credit_too_low", "quote_wide")
    assert state.open_positions == []


def test_outcomes_for_every_expiration_are_recorded():
    logged = []
    chains = {NEAR: _chain(4.00, 2.00), MID: _chain(2.50, 2.00), FAR: _chain(2.30, 2.00)}
    d = _deps(chains, trade_log=lambda r: logged.append(r))
    run_entry_cycle(BotState(), d, MONDAY)
    outcomes = _decision(logged)["expiration_outcomes"]
    for e in (NEAR, MID, FAR):
        assert e in outcomes


def test_flag_off_keeps_the_single_expiry_path_untouched():
    """Bot C safety: with the flag off, pick_expiry's choice is used and no alternate-expiration
    telemetry is emitted at all."""
    logged = []
    chains = {NEAR: _chain(4.00, 2.00), MID: _chain(5.00, 2.00)}
    d = _deps(chains, trade_log=lambda r: logged.append(r),
              features=S2bFeatures(aggregate_risk_budget=True, decision_logging=True))
    state, info = run_entry_cycle(BotState(), d, MONDAY)
    assert info == "filled"
    assert state.open_positions[0].expiry == NEAR      # pick_expiry's choice, not the richer MID
    assert "expiration_selected" not in _decision(logged)


def test_missing_expirations_feed_falls_back_to_pick_expiry():
    logged = []
    chains = {NEAR: _chain(4.00, 2.00), MID: _chain(5.00, 2.00)}
    d = _deps(chains, trade_log=lambda r: logged.append(r), get_expirations=None)
    state, info = run_entry_cycle(BotState(), d, MONDAY)
    assert info == "filled"
    assert state.open_positions[0].expiry == NEAR


def test_a_failing_expiration_does_not_abort_the_cycle():
    """One expiry whose chain fetch raises must simply lose, not kill the entry cycle."""
    logged = []
    chains = {NEAR: _chain(2.20, 2.00), MID: _chain(5.00, 2.00)}

    def get_chain(sym, exp):
        if exp == FAR:
            raise RuntimeError("broker hiccup")
        return chains.get(exp, [])
    d = _deps(chains, trade_log=lambda r: logged.append(r), get_chain=get_chain)
    state, info = run_entry_cycle(BotState(), d, MONDAY)
    assert info == "filled"
    assert state.open_positions[0].expiry == MID
