"""Adaptive credit-tier sizing wired into the entry cycle (partner review v2 §3). OPT-IN via
deps.features.credit_tiers (False = feature OFF, byte-identical to prior behavior). Revises the
Task 1-2 `deps.min_credit_ratio` wiring: sizes off `exec_credit` (the conservative
expected-executable credit, §4) rather than the naive mid-quote credit, adds a ceiling-clamped
adaptive threshold gated on >= credit_adapt_min_signals prior candidates, and records EVERY
evaluated candidate (including ones about to be rejected) into state.credit_ratio_history."""
import dataclasses
from datetime import datetime

import pytest

from bot.app.orchestrator import BotState, Deps, run_entry_cycle
from bot.strategy.s2b import OptionQuote
from bot.risk_gate import AccountState
from bot.features import S2bFeatures


def _custom_chain(short_bid, long_ask):
    """Minimal 568/558 SPY chain (spot=575, atr=6.0). natural credit = short_bid - long_ask;
    the 10-cent bid/ask spread on each leg makes package_mid = natural + 0.10, so with the
    default expected_entry_slippage=0.03, exec_credit = natural + 0.07 for every fixture below."""
    return [
        OptionQuote(568.0, 0.36, short_bid, short_bid + 0.10),
        OptionQuote(558.0, 0.18, long_ask - 0.10, long_ask),
    ]


def _acct(today, conc):
    return AccountState(20_000.0, 20_000.0, 0.0, conc, 0.0, {}, today)


def _deps(**over):
    base = dict(
        get_spot=lambda sym: 575.0, get_atr=lambda sym: 6.0,
        get_chain=lambda sym, exp: [], pick_expiry=lambda today: "2026-06-19",
        get_vix_regime=lambda: (0.5, 0.01), account_state=_acct,
        mark_position=lambda p: 3.0, dte_of=lambda p, today: 5,
        open_spread=lambda payload: "filled", close_spread=lambda p, a: "filled",
        broker_positions=lambda: [], broker_equity=lambda: 20_000.0,
        bot_equity=lambda: 20_000.0, alert_sink=lambda alerts: None,
    )
    base.update(over)
    return Deps(**base)


MONDAY = datetime(2026, 6, 15, 10, 5)   # Monday 10:05, expiry 2026-06-19 -> dte=4 -> bucket "4-5"


# ── (a) below min_credit_ratio (exec_credit-based) -> rejected, nothing opened ──────────────────

def test_credit_ratio_below_min_rejects_entry():
    state = BotState()
    # natural=0.90 -> exec_credit=0.97 -> ratio=0.097 < min_credit_ratio(0.10)
    d = _deps(get_chain=lambda sym, exp: _custom_chain(3.00, 2.10),
              features=S2bFeatures(credit_tiers=True))
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "credit_too_low"
    assert state.open_positions == []


# ── (b) just above min but below full threshold -> probe (reduced) size, legacy (non-risk-budget) path ──

def test_credit_ratio_probe_size_reduced_qty_legacy_path():
    state = BotState()
    # natural=1.00 -> exec_credit=1.07 -> ratio=0.107, between min(0.10) and warmup floor(0.115)
    d = _deps(get_chain=lambda sym, exp: _custom_chain(3.10, 2.10),
              features=S2bFeatures(credit_tiers=True))
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    assert len(state.open_positions) == 1
    # order.credit=1.00 -> max_loss=(10-1.00)*100=900; contracts_for_risk(20k, 900, 0.10)=floor(2000/900)=2
    # probe_size_multiplier defaults to 0.40 -> max(1, int(2*0.40)) == 1
    assert state.open_positions[0].qty == 1


# ── (c) well above full threshold -> full size (unchanged from base sizing), legacy path ────────

def test_credit_ratio_full_size_when_above_threshold_legacy_path():
    state = BotState()
    # natural=2.00 -> exec_credit=2.07 -> ratio=0.207, comfortably above the warmup floor
    d = _deps(get_chain=lambda sym, exp: _custom_chain(4.00, 2.00),
              features=S2bFeatures(credit_tiers=True))
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    assert len(state.open_positions) == 1
    # order.credit=2.00 -> max_loss=(10-2.00)*100=800 -> floor(2000/800)=2; full size, no reduction
    assert state.open_positions[0].qty == 2


# ── (d) credit_ratio_history grows in the right DTE bucket, keyed on exec_credit, on a fill ─────

def test_credit_ratio_history_grows_on_fill():
    state = BotState()
    d = _deps(get_chain=lambda sym, exp: _custom_chain(4.00, 2.00),
              features=S2bFeatures(credit_tiers=True))
    assert state.credit_ratio_history == {}
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    assert state.credit_ratio_history.get("4-5") == [0.207]   # exec_credit(2.07)/wing(10)


# ── (e) §3: rejected candidates ARE counted (no look-ahead: appended BEFORE the reject check runs) ──

def test_credit_ratio_history_records_rejected_candidates_too():
    state = BotState()
    d = _deps(get_chain=lambda sym, exp: _custom_chain(3.00, 2.10),   # exec_credit=0.97 -> reject
              features=S2bFeatures(credit_tiers=True))
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "credit_too_low"
    assert state.credit_ratio_history.get("4-5") == [0.097]


# ── (f) adaptive threshold only kicks in at >= credit_adapt_min_signals prior candidates ────────

def test_adaptive_threshold_uses_floor_when_under_min_signals():
    state = BotState()
    # natural=1.23 -> exec_credit=1.30 -> ratio=0.130; warmup floor(0.115) -> full size (0.130 > 0.115)
    d = _deps(get_chain=lambda sym, exp: _custom_chain(3.23, 2.00),
              features=S2bFeatures(credit_tiers=True))
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    # order.credit=1.23 -> max_loss=(10-1.23)*100=877 -> floor(2000/877)=2; full size -> qty=2
    assert state.open_positions[0].qty == 2


def test_adaptive_threshold_adapts_to_ceiling_once_min_signals_reached():
    state = BotState()
    state.credit_ratio_history["4-5"] = [0.20] * 30   # >= credit_adapt_min_signals(30) -> p40=0.20,
                                                       # clamped to full_size_credit_ceiling(0.14)
    d = _deps(get_chain=lambda sym, exp: _custom_chain(3.23, 2.00),   # same candidate as above: ratio=0.130
              features=S2bFeatures(credit_tiers=True))
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    # now 0.130 < adapted threshold(0.14) -> probe: max(1, int(2*0.40)) == 1 (vs full size when unseeded)
    assert state.open_positions[0].qty == 1
    assert state.credit_ratio_history["4-5"][-1] == 0.13


# ── (g) probe multiplier flows into the aggregate-risk-budget qty (partner review v2 §9 integration) ──

def test_probe_multiplier_flows_into_risk_budget_qty():
    state = BotState()
    d = _deps(get_chain=lambda sym, exp: _custom_chain(3.10, 2.10),   # exec_credit=1.07 -> ratio=0.107 -> probe
              features=S2bFeatures(credit_tiers=True, aggregate_risk_budget=True),
              risk_equity=lambda: 100_000.0,
              account_state=lambda today, conc: AccountState(100_000.0, 100_000.0, 0.0, conc, 0.0, {}, today),
              broker_equity=lambda: 100_000.0, bot_equity=lambda: 100_000.0)
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    # size_qty(100k, credit=exec_credit=1.07, wing=10, f, quality_multiplier=0.40):
    #   psl=min(893, 224)=224 -> qty_entry=floor(750/224)=3; struct=893 -> qty_structural=floor(5000/893)=5
    #   floor(min(3,5)*0.40) = floor(1.2) = 1
    assert state.open_positions[0].qty == 1


def test_full_size_multiplier_is_a_no_op_in_risk_budget_qty():
    state = BotState()
    d = _deps(get_chain=lambda sym, exp: _custom_chain(4.00, 2.00),   # exec_credit=2.07 -> full size (mult=1.0)
              features=S2bFeatures(credit_tiers=True, aggregate_risk_budget=True),
              risk_equity=lambda: 100_000.0,
              account_state=lambda today, conc: AccountState(100_000.0, 100_000.0, 0.0, conc, 0.0, {}, today),
              broker_equity=lambda: 100_000.0, bot_equity=lambda: 100_000.0)
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    # size_qty(100k, credit=2.07, wing=10, f, quality_multiplier=1.0):
    #   psl=min(struct=793, (2*2.07+0.10)*100=424)=424 -> qty_entry=floor(750/424)=1
    #   struct=793 -> qty_structural=floor(5000/793)=6 -> floor(min(1,6)*1.0)=1
    assert state.open_positions[0].qty == 1


# ── (h) decision log carries credit_ratio/credit_pctl40/credit_sample_count/credit_threshold/credit_quality_mult ──

def test_decision_log_carries_credit_telemetry():
    logged = []
    d = _deps(get_chain=lambda sym, exp: _custom_chain(3.00, 2.10),   # exec_credit=0.97 -> reject
              features=S2bFeatures(credit_tiers=True, decision_logging=True),
              trade_log=lambda rec: logged.append(rec))
    state = BotState()
    state.credit_ratio_history["4-5"] = [0.10, 0.20]   # count=2, p40=_percentile([0.10,0.20],40)=0.14
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "credit_too_low"
    decisions = [r for r in logged if r["event"] == "DECISION"]
    assert len(decisions) == 1
    rec = decisions[0]
    assert rec["credit_ratio"] == pytest.approx(0.097)
    assert rec["credit_sample_count"] == 2
    assert rec["credit_pctl40"] == pytest.approx(0.14)
    assert rec["credit_threshold"] == 0.115   # count(2) < min_signals(30) -> floor
    # Post-v2 refinement §1: ratio 0.097 sits in the [0.08, 0.10) low_credit_safety BAND, so
    # classify_credit_quality returns that lane's 0.25x multiplier -- NOT the old flat 0.0 reject
    # (only ratio < ABSOLUTE_CREDIT_FLOOR 0.08 classifies as "reject" now). The candidate is still
    # REJECTED here, by the strict low_credit_safety_pass gate that follows: entry_delta is None on
    # this chain, and that gate FAILS CLOSED without a short delta. So the multiplier telemetry
    # reports the CLASSIFICATION (0.25) while decision_outcome reports the OUTCOME (blocked).
    assert rec["credit_quality_mult"] == 0.25
    assert rec["decision_outcome"] == "blocked_credit_quality"


# ── (i) feature OFF by default -> byte-identical behavior, no history, no telemetry ─────────────

def test_credit_tiers_feature_off_by_default_no_gating():
    # a ratio that WOULD be rejected under credit_tiers=True must open normally when the feature
    # is off (the default), at unreduced qty, with no history recorded.
    state = BotState()
    d = _deps(get_chain=lambda sym, exp: _custom_chain(3.00, 2.10))   # credit=0.90, ratio=0.09
    assert d.features.credit_tiers is False
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    assert len(state.open_positions) == 1
    # base sizing: max_loss=(10-0.90)*100=910 -> floor(2000/910)=2, unreduced
    assert state.open_positions[0].qty == 2
    assert state.credit_ratio_history == {}


def test_features_credit_defaults():
    f = S2bFeatures()
    assert f.credit_tiers is False
    assert f.min_credit_ratio == 0.10
    assert f.full_size_credit_floor == 0.115
    assert f.full_size_credit_ceiling == 0.14
    assert f.probe_size_multiplier == 0.40
    assert f.credit_adapt_min_signals == 30


def test_botstate_credit_ratio_history_default_empty_dict():
    s = BotState()
    assert s.credit_ratio_history == {}


def test_deps_no_longer_carries_legacy_credit_fields():
    names = {f.name for f in dataclasses.fields(Deps)}
    assert "min_credit_ratio" not in names
    assert "credit_floor_ratio" not in names
    assert "probe_size_multiplier" not in names
    assert "use_adaptive_credit" not in names


# ── (j) Priority-0 fix item 3: dedup repeated near-identical polling-cycle observations of the
# SAME candidate so a 240-cycle day doesn't flood the 60-signal history with duplicates ──────────

def test_credit_ratio_history_dedups_repeated_identical_polling_cycles():
    state = BotState()
    # exec_credit=0.97 -> ratio=0.097 (< min_credit_ratio 0.10) -> always rejected, so open_positions
    # / entries_today never change and every one of the 50 cycles reaches the credit-tiers block.
    d = _deps(get_chain=lambda sym, exp: _custom_chain(3.00, 2.10),
              features=S2bFeatures(credit_tiers=True))
    for _ in range(50):
        state, info = run_entry_cycle(state, d, MONDAY)
        assert info == "credit_too_low"
    # 50 identical polling cycles of the SAME candidate in the SAME 15-min window -> at most 1
    # history entry (proves the 240-cycle flooding bug is fixed).
    assert state.credit_ratio_history.get("4-5") == [0.097]

    # a genuine ratio move >= 0.5pp (still same strikes/expiry/window) -> DOES get recorded.
    # exec_credit=0.92 -> ratio=0.092; |0.092 - 0.097| = 0.005 >= 0.005 threshold.
    d2 = _deps(get_chain=lambda sym, exp: _custom_chain(2.95, 2.10),
               features=S2bFeatures(credit_tiers=True))
    state, info = run_entry_cycle(state, d2, MONDAY)
    assert info == "credit_too_low"
    assert state.credit_ratio_history["4-5"] == [0.097, 0.092]

    # repeating that SAME candidate again (same ratio, same window) does NOT record again.
    state, info = run_entry_cycle(state, d2, MONDAY)
    assert info == "credit_too_low"
    assert state.credit_ratio_history["4-5"] == [0.097, 0.092]

    # a new 15-minute research window (same candidate/ratio) DOES get recorded, even though nothing
    # about the spread itself changed.
    later = MONDAY.replace(minute=21)   # bucket15 moves from 2 (10:05) to 3 (10:21)
    state, info = run_entry_cycle(state, d2, later)
    assert info == "credit_too_low"
    assert state.credit_ratio_history["4-5"] == [0.097, 0.092, 0.092]

    # ...and repeating THAT identical candidate/window many more times still only records once.
    for _ in range(50):
        state, info = run_entry_cycle(state, d2, later)
        assert info == "credit_too_low"
    assert state.credit_ratio_history["4-5"] == [0.097, 0.092, 0.092]
