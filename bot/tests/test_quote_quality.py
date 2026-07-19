"""Advisor directive Step 1: plumb the two missing low-credit-safety inputs.

The 0.08-0.10 low_credit_safety lane (spec §1) has been shipped but not fully operational: quote age
and expected-move cushion were unplumbed, so the lane failed closed and a thin-credit candidate could
only ever qualify through the short-delta branch of the OR.

    low_credit_safety_pass = (
        target_to_cost_ratio >= 4.5
        and package_width_ratio <= 0.20
        and quote_freshness_pass
        and cushion_atr >= 1.15
        and (expected_move_pass or short_delta <= 0.30)
        and not defensive_market_state
    )

Both new inputs FAIL CLOSED: an age that cannot be determined is not fresh, and an expected move that
cannot be computed is not a pass. Critically, the current time is never substituted for a missing
quote timestamp -- that would make stale cached data look perfectly fresh, which is the precise
failure this gate exists to prevent.
"""
from datetime import datetime, timedelta

import pytest

from bot.strategy.quote_quality import (calculate_quote_age_seconds, spread_quote_age_seconds,
                                         quote_freshness_pass, calculate_atm_iv,
                                         expected_move_to_expiry, expected_move_cushion_of,
                                         MAX_QUOTE_AGE_SECONDS, MIN_EXPECTED_MOVE_CUSHION)
from bot.strategy.s2b import OptionQuote


NOW = datetime(2026, 7, 20, 10, 0, 0)


def _q(strike=568.0, bid=4.0, ask=4.1, exch=None, recv=None, iv=None):
    return OptionQuote(strike=strike, delta=0.36, bid=bid, ask=ask,
                       exchange_timestamp=exch, received_timestamp=recv, iv=iv)


# ── 1A: quote age ────────────────────────────────────────────────────────────────────────────────

def test_exchange_timestamp_is_preferred_over_received():
    q = _q(exch=NOW - timedelta(seconds=5), recv=NOW - timedelta(seconds=1))
    assert calculate_quote_age_seconds(q, NOW) == 5.0


def test_falls_back_to_received_timestamp_when_exchange_is_absent():
    assert calculate_quote_age_seconds(_q(recv=NOW - timedelta(seconds=3)), NOW) == 3.0


def test_missing_timestamp_returns_none_not_zero():
    """None, never 0.0. Returning 0.0 would read as "perfectly fresh" -- exactly backwards for a
    quote whose age is unknown, and the trap the advisor called out about cached data."""
    assert calculate_quote_age_seconds(_q(), NOW) is None


def test_clock_skew_cannot_produce_a_negative_age():
    assert calculate_quote_age_seconds(_q(exch=NOW + timedelta(seconds=4)), NOW) == 0.0


def test_spread_age_takes_the_older_of_the_two_legs():
    short = _q(exch=NOW - timedelta(seconds=1))
    long_ = _q(exch=NOW - timedelta(seconds=6))
    assert spread_quote_age_seconds(short, long_, NOW) == 6.0


def test_spread_age_is_none_when_either_leg_is_unknown():
    """One unknown leg poisons the pair: max() over a None is meaningless, and treating the known leg
    as the answer would let a stale-but-untimestamped leg ride through."""
    assert spread_quote_age_seconds(_q(exch=NOW), _q(), NOW) is None
    assert spread_quote_age_seconds(_q(), _q(exch=NOW), NOW) is None


def test_freshness_threshold_is_two_seconds_inclusive():
    assert MAX_QUOTE_AGE_SECONDS == 2.0
    assert quote_freshness_pass(2.0) is True
    assert quote_freshness_pass(2.01) is False
    assert quote_freshness_pass(0.0) is True


def test_unknown_age_fails_freshness_closed():
    assert quote_freshness_pass(None) is False


def test_stale_one_leg_quote_fails_the_pair():
    fresh, stale = _q(exch=NOW), _q(exch=NOW - timedelta(seconds=30))
    assert quote_freshness_pass(spread_quote_age_seconds(fresh, stale, NOW)) is False


def test_fresh_two_leg_quote_passes():
    a = _q(exch=NOW - timedelta(seconds=1))
    b = _q(exch=NOW - timedelta(seconds=1.5))
    assert quote_freshness_pass(spread_quote_age_seconds(a, b, NOW)) is True


# ── 1B: ATM IV and expected move ─────────────────────────────────────────────────────────────────

def test_atm_iv_averages_both_sides_when_both_valid():
    assert calculate_atm_iv(0.20, 0.24) == pytest.approx(0.22)


def test_atm_iv_uses_the_valid_side_when_only_one_is_available():
    assert calculate_atm_iv(None, 0.24) == 0.24
    assert calculate_atm_iv(0.20, None) == 0.20


def test_atm_iv_rejects_nonpositive_and_missing():
    assert calculate_atm_iv(None, None) is None
    assert calculate_atm_iv(0.0, None) is None
    assert calculate_atm_iv(-0.1, 0.0) is None


def test_expected_move_matches_the_specified_formula():
    # 575 * 0.20 * sqrt(5/365)
    import math
    assert expected_move_to_expiry(575.0, 0.20, 5) == pytest.approx(
        575.0 * 0.20 * math.sqrt(5 / 365.0))


@pytest.mark.parametrize("spot,iv,dte", [(0.0, 0.2, 5), (-1.0, 0.2, 5), (575.0, 0.0, 5),
                                          (575.0, -0.2, 5), (575.0, 0.2, 0), (575.0, 0.2, -3)])
def test_expected_move_fails_closed_on_invalid_inputs(spot, iv, dte):
    assert expected_move_to_expiry(spot, iv, dte) is None


def test_expected_move_cushion_is_strike_distance_over_expected_move():
    # spot 575, short 560 -> cushion 15; expected move 15 -> ratio 1.0
    assert expected_move_cushion_of(spot=575.0, short_strike=560.0, expected_move=15.0) == 1.0


def test_expected_move_cushion_is_none_when_the_move_is_unavailable():
    assert expected_move_cushion_of(spot=575.0, short_strike=560.0, expected_move=None) is None
    assert expected_move_cushion_of(spot=575.0, short_strike=560.0, expected_move=0.0) is None


def test_cushion_threshold_is_zero_point_nine_inclusive():
    assert MIN_EXPECTED_MOVE_CUSHION == 0.90


# ── the assembled low-credit conjunction ─────────────────────────────────────────────────────────

from bot.strategy.credit_quality import low_credit_safety_pass


def _pass_kwargs(**over):
    base = dict(target_to_cost_ratio=5.0, package_width_ratio=0.10, quote_age_seconds=1.0,
                cushion_atr=1.5, expected_move_cushion=1.0, short_delta=0.40,
                defensive_market_state=False)
    base.update(over)
    return base


def test_all_conditions_met_passes():
    assert low_credit_safety_pass(**_pass_kwargs()) is True


def test_expected_move_cushion_exactly_at_threshold_passes():
    assert low_credit_safety_pass(**_pass_kwargs(expected_move_cushion=0.90)) is True


def test_short_delta_fallback_qualifies_when_expected_move_is_unavailable():
    """The OR branch: no IV feed (cushion None/0) but a sufficiently far short strike."""
    assert low_credit_safety_pass(**_pass_kwargs(expected_move_cushion=None,
                                                  short_delta=0.30)) is True


def test_fails_when_neither_expected_move_nor_delta_qualifies():
    assert low_credit_safety_pass(**_pass_kwargs(expected_move_cushion=0.89,
                                                  short_delta=0.31)) is False


def test_missing_quote_age_fails_the_conjunction():
    assert low_credit_safety_pass(**_pass_kwargs(quote_age_seconds=None)) is False


def test_stale_quote_fails_the_conjunction():
    assert low_credit_safety_pass(**_pass_kwargs(quote_age_seconds=2.5)) is False


@pytest.mark.parametrize("over", [
    dict(target_to_cost_ratio=4.49), dict(package_width_ratio=0.21),
    dict(cushion_atr=1.14), dict(defensive_market_state=True),
])
def test_each_hard_condition_can_veto(over):
    assert low_credit_safety_pass(**_pass_kwargs(**over)) is False


# ── end-to-end: the lane is genuinely operational in run_entry_cycle ─────────────────────────────

from bot.app.orchestrator import BotState, Deps, run_entry_cycle
from bot.risk_gate import AccountState
from bot.features import S2bFeatures


def _lc_chain(now, iv=0.12, age_s=1.0, long_age_s=None):
    """Thin-credit 560/550 chain: credit 0.90 on a $10 wing -> ratio 0.09, inside the 0.08-0.10
    low_credit_safety band. A 570 strike near spot 575 supplies the ATM IV."""
    ts = now - timedelta(seconds=age_s)
    lts = now - timedelta(seconds=long_age_s if long_age_s is not None else age_s)
    return [
        OptionQuote(570.0, 0.45, 5.00, 5.10, exchange_timestamp=ts, iv=iv),
        OptionQuote(560.0, 0.28, 1.40, 1.50, exchange_timestamp=ts, iv=iv),
        OptionQuote(550.0, 0.18, 0.50, 0.60, exchange_timestamp=lts, iv=iv),
    ]


def _lc_deps(chain, **over):
    base = dict(
        get_spot=lambda sym: 575.0, get_atr=lambda sym: 6.0,
        get_chain=lambda sym, exp: chain,
        pick_expiry=lambda today: "2026-07-24",
        get_vix_regime=lambda: (0.5, 0.01),
        account_state=lambda today, conc: AccountState(100_000.0, 100_000.0, 0.0, conc, 0.0, {}, today),
        mark_position=lambda p: 3.0, dte_of=lambda p, today: 4,
        open_spread=lambda payload: "filled", close_spread=lambda p, a: "filled",
        broker_positions=lambda: [], broker_equity=lambda: 100_000.0,
        bot_equity=lambda: 100_000.0, alert_sink=lambda alerts: None,
        risk_equity=lambda: 100_000.0,
        features=S2bFeatures(credit_tiers=True, decision_logging=True),
    )
    base.update(over)
    return Deps(**base)


LC_NOW = datetime(2026, 7, 20, 10, 5)


def _lc_decision(logged):
    return next(r for r in logged if r.get("event") == "DECISION")


def test_low_credit_lane_populates_the_real_inputs_end_to_end():
    """The point of Step 1: these fields must carry REAL values, not the old 0.0 placeholders."""
    logged = []
    d = _lc_deps(_lc_chain(LC_NOW), trade_log=lambda r: logged.append(r))
    run_entry_cycle(BotState(), d, LC_NOW)
    rec = _lc_decision(logged)
    assert rec["lc_spread_quote_age"] == 1.0
    assert rec["lc_quote_time_source"] == "exchange"
    assert rec["lc_quote_fresh"] is True
    assert rec["lc_atm_iv"] == 0.12                    # from the 570 strike nearest spot 575
    assert rec["lc_expected_move"] is not None
    assert rec["lc_expected_move_cushion"] is not None


def test_low_credit_candidate_fails_closed_when_the_feed_supplies_no_timestamp():
    """A chain with no quote timestamps at all: age unknown -> not fresh -> lane blocked, even though
    every other condition would pass. Previously this path hardcoded 0.0 and read as fresh."""
    logged = []
    chain = [OptionQuote(570.0, 0.45, 5.00, 5.10, iv=0.12),
             OptionQuote(560.0, 0.28, 1.40, 1.50, iv=0.12),
             OptionQuote(550.0, 0.18, 0.50, 0.60, iv=0.12)]
    d = _lc_deps(chain, trade_log=lambda r: logged.append(r))
    state, info = run_entry_cycle(BotState(), d, LC_NOW)
    rec = _lc_decision(logged)
    assert rec["lc_spread_quote_age"] is None
    assert rec["lc_quote_time_source"] == "none"
    assert rec["lc_quote_fresh"] is False
    assert rec["lc_safety_pass"] is False
    assert info == "credit_too_low"
    assert state.open_positions == []


def test_low_credit_candidate_fails_closed_when_one_leg_is_stale():
    logged = []
    d = _lc_deps(_lc_chain(LC_NOW, age_s=1.0, long_age_s=45.0),
                 trade_log=lambda r: logged.append(r))
    state, info = run_entry_cycle(BotState(), d, LC_NOW)
    rec = _lc_decision(logged)
    assert rec["lc_spread_quote_age"] == 45.0        # the OLDER leg governs
    assert rec["lc_quote_fresh"] is False
    assert info == "credit_too_low"


def test_missing_iv_leaves_the_lane_on_the_delta_fallback():
    """No IV in the chain -> no expected move -> cushion None. The lane must not crash; it simply
    depends on the short-delta branch, which is what Step 1B set out to stop being the ONLY route."""
    logged = []
    d = _lc_deps(_lc_chain(LC_NOW, iv=None), trade_log=lambda r: logged.append(r))
    run_entry_cycle(BotState(), d, LC_NOW)
    rec = _lc_decision(logged)
    assert rec["lc_atm_iv"] is None
    assert rec["lc_expected_move"] is None
    assert rec["lc_expected_move_cushion"] is None
