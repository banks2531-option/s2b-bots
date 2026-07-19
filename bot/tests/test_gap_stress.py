"""Gap-risk stress test (partner review v2 §10): reprice spreads at SPY down 1/1.5/2 ATR."""
import pytest

from bot.portfolio.gap_stress import stressed_spread_loss, gap_stress_losses, gap_stress_ok
from bot.strategy.manage import ManagedPosition
from bot.features import S2bFeatures
from bot.portfolio.exposure import _ForeignSpread


# ── stressed_spread_loss: caps at wing width; negative (profit) when still OTM ─

def test_stressed_spread_loss_caps_at_wing_width():
    # short=100, long=90 (actual strike diff = 10), but wing_width param is 8 (< strike diff),
    # and the drop puts both strikes fully ITM (diff would be 10 uncapped) -> capped to 8.
    # spot=120, atr=10, drop=3 -> S=120-30=90; short-S=10, long-S=0 -> diff=10, capped to 8.
    loss = stressed_spread_loss(100.0, 90.0, credit=2.0, qty=1, spot=120.0, atr=10.0,
                                 drop_atr=3.0, wing_width=8.0)
    assert loss == pytest.approx((8.0 - 2.0) * 100.0)


def test_stressed_spread_loss_negative_profit_when_both_strikes_otm():
    # spot=575, atr=6, drop=1.0 -> S=569; short=568 < S (OTM put), long=558 < S (OTM) -> diff=0.
    loss = stressed_spread_loss(568.0, 558.0, credit=2.0, qty=3, spot=575.0, atr=6.0,
                                 drop_atr=1.0, wing_width=10.0)
    assert loss == pytest.approx((0.0 - 2.0) * 100.0 * 3)
    assert loss < 0


def test_stressed_spread_loss_partial_itm_no_cap_needed():
    # spot=575, atr=6, drop=1.5 -> S=566; short=568 ITM by 2, long=558 still OTM (0) -> diff=2.
    loss = stressed_spread_loss(568.0, 558.0, credit=0.5, qty=2, spot=575.0, atr=6.0,
                                 drop_atr=1.5, wing_width=10.0)
    assert loss == pytest.approx((2.0 - 0.5) * 100.0 * 2)


# ── BS shock-grid reprice (Priority-0 fix item 5c): worse-than-intrinsic loss ──

def test_bs_grid_reports_larger_loss_than_intrinsic_for_743_733():
    from bot.portfolio.gap_stress import stressed_spread_loss_bs, stressed_spread_loss
    f = S2bFeatures()
    kw = dict(short_strike=743, long_strike=733, credit=1.39, qty=8, spot=754.88, atr=8.58)
    intrinsic = stressed_spread_loss(**kw, drop_atr=1.5)
    bs = stressed_spread_loss_bs(**kw, drop_atr=1.5, dte=9, short_iv=0.18, long_iv=0.20, f=f)
    assert bs > intrinsic          # BS shows a worse (larger) loss than intrinsic
    # Pin the exact worst-cell $ (0.18/0.20 IVs, default grid) so a cell-selection or qty-scaling
    # regression is caught, not just the inequality above.
    assert bs == pytest.approx(3901.65, abs=0.01)


# ── gap_stress_losses: sums across positions, for all three drops ──────────────

def _pos(short, long_, credit, qty):
    return ManagedPosition("SPY", short, long_, credit, qty, "2026-07-18", entry_date="2026-07-10")


# ── gap_quantity_cap: incremental candidate-loss quantity model (spec §8) ──────────────────────

def test_gap_gate_uses_incremental_candidate_loss():
    from bot.portfolio.gap_stress import gap_quantity_cap
    cap = gap_quantity_cap("gap_1_5atr", current_book_loss=3000, one_contract_book_loss=3250,
                           loss_limit=3600)
    assert cap.incremental_risk_per_contract == 250
    assert cap.maximum_qty == 2


def test_gap_quantity_cap_zero_incremental_imposes_no_constraint():
    """DELIBERATE DEVIATION from spec §8, which says `qty = floor(remaining/incremental) when
    incremental > 0 else 0`.

    Taken literally, that rejects every candidate that adds NO stressed loss -- i.e. the gate blocks
    precisely the spreads that cannot lose money in the stress scenario (verified: a deep-OTM 520/510
    at spot 575 with a $2.00 credit returned maximum_qty 0 from all three scenarios). The `else 0`
    reads as a safe-looking guard against dividing by zero rather than a considered decision about
    risk-free candidates, and its literal effect is the opposite of the gate's purpose.

    So zero marginal risk now means "this gate imposes no constraint". It is NOT a licence to trade
    unlimited size: the four aggregate budgets still bind (see
    test_a_risk_free_candidate_still_obeys_the_other_caps), so the worst case is that gap stress
    abstains and the other caps size the trade."""
    from bot.portfolio.gap_stress import gap_quantity_cap
    cap = gap_quantity_cap("gap_1atr", current_book_loss=1000, one_contract_book_loss=1000,
                           loss_limit=5000)
    assert cap.incremental_risk_per_contract == 0.0
    assert cap.maximum_qty is None          # None == abstains, NOT 0 == permits nothing
    assert cap.remaining_capacity == 4000


def test_gap_quantity_cap_book_already_over_limit_gives_zero():
    from bot.portfolio.gap_stress import gap_quantity_cap
    cap = gap_quantity_cap("gap_2atr", current_book_loss=4000, one_contract_book_loss=4300,
                           loss_limit=3600)
    assert cap.remaining_capacity == 0.0        # clamped, never negative
    assert cap.maximum_qty == 0


# ── StressScenario matrix + STRESSED_CLOSE_SLIPPAGE (spec §9) ──────────────────────────────────

def test_stress_scenarios_matrix_matches_spec():
    from bot.portfolio.gap_stress import STRESS_SCENARIOS, STRESSED_CLOSE_SLIPPAGE
    assert STRESSED_CLOSE_SLIPPAGE == 0.10
    triples = [(s.name, s.atr_move, s.iv_point_change) for s in STRESS_SCENARIOS]
    assert triples == [
        ("down_1atr", -1.0, 3.0),
        ("down_1_5atr", -1.5, 5.0),
        ("down_2atr", -2.0, 10.0),
    ]


def test_stressed_spread_loss_bs_default_unchanged_no_scenario_params():
    # Back-compat guard: without iv_point_change / stressed_close_slippage the BS reprice is
    # byte-identical to before (same pinned worst-cell $ as test_bs_grid_reports_larger_loss...).
    from bot.portfolio.gap_stress import stressed_spread_loss_bs
    f = S2bFeatures()
    kw = dict(short_strike=743, long_strike=733, credit=1.39, qty=8, spot=754.88, atr=8.58)
    bs = stressed_spread_loss_bs(**kw, drop_atr=1.5, dte=9, short_iv=0.18, long_iv=0.20, f=f)
    assert bs == pytest.approx(3901.65, abs=0.01)


def test_stressed_spread_loss_bs_scenario_iv_and_slippage_raise_loss():
    # Driving the IV bump from a scenario point-change AND adding close slippage produces a loss
    # >= the plain default (more IV / added slippage can only worsen a bull-put stressed close).
    from bot.portfolio.gap_stress import stressed_spread_loss_bs, STRESSED_CLOSE_SLIPPAGE
    f = S2bFeatures()
    kw = dict(short_strike=743, long_strike=733, credit=1.39, qty=8, spot=754.88, atr=8.58,
              drop_atr=1.5, dte=9, short_iv=0.18, long_iv=0.20, f=f)
    base = stressed_spread_loss_bs(**kw)
    scenario = stressed_spread_loss_bs(**kw, iv_point_change=10.0,
                                       stressed_close_slippage=STRESSED_CLOSE_SLIPPAGE)
    assert scenario > base


# ── gap_quantity_caps: three scenarios, strictest binds (spec §8/§9) ───────────────────────────

def test_gap_quantity_caps_three_scenarios_bigger_candidate_smaller_cap():
    from bot.portfolio.gap_stress import gap_quantity_caps
    f = S2bFeatures()
    iv_fn = lambda p: (0.18, 0.20)
    # candidate near spot (568 short) vs one further OTM (560 short) -> nearer stresses harder.
    near = _pos(568.0, 558.0, 2.0, 1)
    far = _pos(560.0, 550.0, 2.0, 1)
    caps_near = gap_quantity_caps([], near, 575.0, 6.0, f, risk_equity=100_000.0,
                                  iv_fn=iv_fn, today="2026-07-09")
    caps_far = gap_quantity_caps([], far, 575.0, 6.0, f, risk_equity=100_000.0,
                                 iv_fn=iv_fn, today="2026-07-09")
    assert [c.name for c in caps_near] == ["gap_1atr", "gap_1_5atr", "gap_2atr"]
    # bigger per-contract stress (nearer candidate) -> smaller permitted qty at the 1.5-ATR scenario
    near_by = {c.name: c for c in caps_near}
    far_by = {c.name: c for c in caps_far}
    assert near_by["gap_1_5atr"].incremental_risk_per_contract > \
        far_by["gap_1_5atr"].incremental_risk_per_contract
    assert near_by["gap_1_5atr"].maximum_qty <= far_by["gap_1_5atr"].maximum_qty


def test_gap_quantity_caps_strictest_scenario_is_binding():
    from bot.portfolio.gap_stress import gap_quantity_caps
    f = S2bFeatures()
    iv_fn = lambda p: (0.18, 0.20)
    candidate = _pos(568.0, 558.0, 2.0, 1)
    caps = gap_quantity_caps([], candidate, 575.0, 6.0, f, risk_equity=100_000.0,
                             iv_fn=iv_fn, today="2026-07-09")
    by = {c.name: c for c in caps}
    # 2.0-ATR has the deepest stress AND the tightest limit pct -> it permits the fewest contracts.
    binding = min(caps, key=lambda c: c.maximum_qty)
    assert binding.name == "gap_2atr"
    assert by["gap_2atr"].maximum_qty <= by["gap_1_5atr"].maximum_qty <= by["gap_1atr"].maximum_qty


def test_gap_quantity_caps_no_iv_fn_uses_fallback_and_is_finite():
    import math
    from bot.portfolio.gap_stress import gap_quantity_caps
    f = S2bFeatures()
    candidate = _pos(568.0, 558.0, 2.0, 1)
    caps = gap_quantity_caps([], candidate, 575.0, 6.0, f, risk_equity=100_000.0,
                             iv_fn=None, today="2026-07-09")
    assert len(caps) == 3
    for c in caps:
        assert math.isfinite(c.incremental_risk_per_contract)


def test_gap_stress_losses_sums_across_positions_all_three_drops():
    positions = [_pos(568.0, 558.0, 2.0, 1), _pos(568.0, 558.0, 0.5, 2)]
    spot, atr, wing = 575.0, 6.0, 10.0
    expected = {}
    for d in (1.0, 1.5, 2.0):
        expected[d] = round(
            stressed_spread_loss(568.0, 558.0, 2.0, 1, spot, atr, d, wing)
            + stressed_spread_loss(568.0, 558.0, 0.5, 2, spot, atr, d, wing), 2)
    got = gap_stress_losses(positions, spot, atr, wing)
    assert got == expected
    assert set(got.keys()) == {1.0, 1.5, 2.0}


def test_gap_stress_losses_empty_positions_all_zero():
    got = gap_stress_losses([], 575.0, 6.0, 10.0)
    assert got == {1.0: 0.0, 1.5: 0.0, 2.0: 0.0}


# ── gap_stress_ok: gated on the 1.5-ATR total loss vs budget ───────────────────

def test_gap_stress_ok_false_when_1_5_atr_loss_exceeds_budget():
    # single thin-credit position: at 1.5 ATR, loss = (2.0-0.2)*100*5 = 900
    positions = [_pos(568.0, 558.0, 0.2, 5)]
    risk_equity = 100_000.0
    # budget pct so tight that 900 > pct*100_000 -> pct < 0.009
    assert gap_stress_ok(positions, 575.0, 6.0, risk_equity, 0.005, 10.0) is False


def test_gap_stress_ok_true_when_1_5_atr_loss_within_budget():
    positions = [_pos(568.0, 558.0, 0.2, 5)]
    risk_equity = 100_000.0
    # default-style budget (0.06) easily covers the 900 loss
    assert gap_stress_ok(positions, 575.0, 6.0, risk_equity, 0.06, 10.0) is True


# ── wired into run_entry_cycle behind deps.features.aggregate_risk_budget ──────

from datetime import datetime

import types

from bot.app.orchestrator import BotState, Deps, run_entry_cycle, _make_gap_iv_resolver
from bot.strategy.s2b import OptionQuote, _occ
from bot.risk_gate import AccountState
from bot.features import S2bFeatures


def _thin_credit_chain(short_bid, long_ask):
    """SPY 568/558 chain (spot=575, atr=6.0) with a controlled thin credit = short_bid - long_ask."""
    return [
        OptionQuote(568.0, 0.36, short_bid, short_bid + 0.10),
        OptionQuote(558.0, 0.18, long_ask - 0.10, long_ask),
    ]


def _acct(today, conc):
    return AccountState(100_000.0, 100_000.0, 0.0, conc, 0.0, {}, today)


def _deps(**over):
    base = dict(
        get_spot=lambda sym: 575.0, get_atr=lambda sym: 6.0,
        get_chain=lambda sym, exp: _thin_credit_chain(1.00, 0.80),  # credit=0.20
        pick_expiry=lambda today: "2026-06-19",
        get_vix_regime=lambda: (0.5, 0.01), account_state=_acct,
        mark_position=lambda p: 3.0, dte_of=lambda p, today: 5,
        open_spread=lambda payload: "filled", close_spread=lambda p, a: "filled",
        broker_positions=lambda: [], broker_equity=lambda: 100_000.0,
        bot_equity=lambda: 100_000.0, alert_sink=lambda alerts: None,
        risk_equity=lambda: 100_000.0,
    )
    base.update(over)
    return Deps(**base)


MONDAY = datetime(2026, 6, 15, 10, 5)   # Monday 10:05, expiry 2026-06-19


def test_orchestrator_gap_stress_reduces_qty_when_budget_tight():
    """Post-v2 refinement §8/§9: gap stress is now an incremental QuantityCap in the min()-of-caps
    pipeline, NOT the old shrink-loop -- and ENFORCEMENT is always Black-Scholes (spec §9: "do not
    use intrinsic value alone"), so `gap_stress_model` no longer steers what is enforced (it still
    steers the OPEN-row gap_stress_* telemetry below). Both live bots run "bs", so this is a
    no-op for them; see the module docstring note on the flag's reduced scope.

    credit=0.20 on 568/558, spot 575, atr 6.0, empty book. A budget of 0.02*100_000 = 2000 admits
    exactly 3 contracts: the BS 1.5-ATR incremental is 602.30/contract (vs the intrinsic model's
    180 -- BS prices the time value, +5 IV points and the stressed close slippage that intrinsic
    ignores), so floor(2000/602.30) = 3. The flanking scenarios stay loose: gap_1atr incremental
    492.29 vs 0.10*100_000 = 10000, gap_2atr 701.08 vs 0.04*100_000 = 4000 -> caps 20 and 5."""
    log = []
    state = BotState()
    f = S2bFeatures(aggregate_risk_budget=True, max_gap_stress_loss_pct=0.02,
                    decision_logging=True)
    d = _deps(features=f, trade_log=lambda rec: log.append(rec))
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    assert len(state.open_positions) == 1
    assert state.open_positions[0].qty == 3
    dec = next(r for r in log if r.get("event") == "DECISION" and r["decision"] == "filled")
    assert dec["decision_outcome"] == "allowed_reduced"
    assert dec["limiting_gate"] == "gap_1_5atr"          # the gap scenario is the binding cap
    assert dec["final_qty"] == 3
    assert dec["risk_incremental_per_contract"] == pytest.approx(602.30, abs=0.01)
    assert dec["risk_limit"] == 2000.0


def test_orchestrator_gap_stress_rejects_when_even_one_contract_fails():
    """A gap budget too tight for even ONE contract still rejects. Post-v2 refinement: gap stress is
    just another cap, so the returned info string is the unified bare "risk_budget" (no production
    code keys on the old "gap_stress" string); the SPECIFIC scenario that bound is preserved -- with
    better attribution than before -- in the logged reason and the `limiting_gate` telemetry."""
    log = []
    state = BotState()
    f = S2bFeatures(aggregate_risk_budget=True, max_gap_stress_loss_pct=0.001,
                    decision_logging=True)
    d = _deps(features=f, trade_log=lambda rec: log.append(rec))
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "risk_budget"
    assert state.open_positions == []
    dec = next(r for r in log if r.get("event") == "DECISION")
    assert dec["decision"] == "risk_budget:gap_1_5atr"
    assert dec["decision_outcome"] == "blocked_zero_capacity"
    assert dec["limiting_gate"] == "gap_1_5atr"
    assert dec["final_qty"] == 0


def test_orchestrator_foreign_position_raises_gap_stress_total():
    """Priority-0 fix item 6: foreign SPY spreads at the broker (not opened by this bot) must be
    included in the gap-stress book, not just this bot's own tracked positions -- so the logged
    gap-stress totals with a foreign position present must exceed the totals with no foreign
    position, all else equal (own entered position/qty unchanged; max_gap_stress_loss_pct loose
    enough that neither run's qty gets shrunk by the gap-stress budget itself)."""
    # Foreign spread close enough to spot to be stressed (loss > 0) at all three drops; distinct
    # strikes from the entered 568/558 spread so it is unambiguously foreign, not an own-qty echo.
    foreign = ManagedPosition("SPY", 572.0, 562.0, credit=0.0, qty=3, expiry="2026-06-19",
                               entry_date="2026-06-10")

    def _run(account_spy_spreads):
        log = []
        state = BotState()
        f = S2bFeatures(aggregate_risk_budget=True, max_gap_stress_loss_pct=1.0)
        d = _deps(features=f, trade_log=lambda rec: log.append(rec),
                  account_spy_spreads=account_spy_spreads)
        state, info = run_entry_cycle(state, d, MONDAY)
        assert info == "filled"
        return log[-1]

    rec_no_foreign = _run(lambda: [])
    rec_with_foreign = _run(lambda: [foreign])

    assert rec_with_foreign["gap_stress_1_0"] > rec_no_foreign["gap_stress_1_0"]
    assert rec_with_foreign["gap_stress_1_5"] > rec_no_foreign["gap_stress_1_5"]
    assert rec_with_foreign["gap_stress_2_0"] > rec_no_foreign["gap_stress_2_0"]


def test_orchestrator_decision_and_open_gap_stress_agree_with_foreign():
    """Priority-0 fix item 6 (logged == enforced): the §12 DECISION row's agg_gap_stress_1_5 must
    equal the §10 OPEN row's gap_stress_1_5 when a foreign position exists -- both must fold the
    foreign spread into the gap-stress book. Scenario keeps exec_credit == order.credit (credit_tiers
    and transaction_cost_gate both OFF) so the proposed leg is stressed at the same credit on both
    paths, isolating the foreign-inclusion consistency (not the exec_credit nuance deferred to Task 5).
    Gap budget is loose (1.0) so neither path shrinks qty."""
    foreign = ManagedPosition("SPY", 572.0, 562.0, credit=0.0, qty=3, expiry="2026-06-19",
                               entry_date="2026-06-10")
    log = []
    state = BotState()
    f = S2bFeatures(aggregate_risk_budget=True, decision_logging=True, max_gap_stress_loss_pct=1.0)
    d = _deps(features=f, trade_log=lambda rec: log.append(rec),
              account_spy_spreads=lambda: [foreign])
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    decision = next(r for r in log if r["event"] == "DECISION" and r["decision"] == "filled")
    open_rec = next(r for r in log if r["event"] == "OPEN")
    assert decision["agg_gap_stress_1_5"] == pytest.approx(open_rec["gap_stress_1_5"])
    # sanity: the foreign spread actually contributes, so this isn't a trivial 0 == 0.
    assert open_rec["gap_stress_1_5"] != 0.0


def test_orchestrator_gap_stress_off_flag_unchanged():
    # aggregate_risk_budget False (default) -> gap-stress logic is a no-op even though it would
    # have blown a tiny budget; legacy sizing path is used and the trade goes through untouched.
    log = []
    state = BotState()
    d = _deps(trade_log=lambda rec: log.append(rec))   # features defaults -> aggregate_risk_budget False
    assert d.features.aggregate_risk_budget is False
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    assert len(state.open_positions) == 1
    rec = log[-1]
    assert "gap_stress_1_0" not in rec
    assert "gap_stress_1_5" not in rec
    assert "gap_stress_2_0" not in rec


# ── BS dispatch in gap_stress_losses / gap_stress_ok (Priority-0 fix item 5d) ───

def test_gap_stress_losses_bs_dispatch_larger_than_intrinsic():
    # (a) Same book/spot/atr: the BS path (f + iv_fn + today supplied) reports a larger 1.5-ATR loss
    # than the intrinsic path (params omitted) -- BS keeps time value the intrinsic model drops.
    f = S2bFeatures()   # gap_stress_model defaults to "bs"
    positions = [_pos(743.0, 733.0, 1.39, 8)]   # _pos sets expiry 2026-07-18 -> dte 9 vs today below
    iv_fn = lambda p: (0.18, 0.20)
    spot, atr, wing = 754.88, 8.58, 10.0
    intrinsic = gap_stress_losses(positions, spot, atr, wing)                       # no f -> intrinsic
    bs = gap_stress_losses(positions, spot, atr, wing, today="2026-07-09", iv_fn=iv_fn, f=f)   # -> BS
    assert bs[1.5] > intrinsic[1.5]
    # gap_stress_ok mirrors the dispatch: with a tiny budget the BS loss trips it False.
    assert gap_stress_ok(positions, spot, atr, 100_000.0, 0.0001, wing,
                         today="2026-07-09", iv_fn=iv_fn, f=f) is False


def test_gap_stress_losses_intrinsic_when_params_omitted_unchanged():
    # (d) Back-compat: omitting today/iv_fn/f (every legacy caller/test) yields byte-identical
    # intrinsic; and even WITH f supplied, a non-"bs" model still falls back to intrinsic.
    positions = [_pos(568.0, 558.0, 2.0, 1), _pos(568.0, 558.0, 0.5, 2)]
    spot, atr, wing = 575.0, 6.0, 10.0
    expected = {}
    for d in (1.0, 1.5, 2.0):
        expected[d] = round(
            stressed_spread_loss(568.0, 558.0, 2.0, 1, spot, atr, d, wing)
            + stressed_spread_loss(568.0, 558.0, 0.5, 2, spot, atr, d, wing), 2)
    assert gap_stress_losses(positions, spot, atr, wing) == expected
    # Advisor Step 2A: the model is now an explicit argument, not a config read. Asking for
    # intrinsic explicitly (the Step 2B comparison path) yields the same intrinsic numbers even with
    # f/iv_fn/today all supplied.
    assert gap_stress_losses(positions, spot, atr, wing, today="2026-07-09",
                             iv_fn=lambda p: (0.2, 0.2), f=S2bFeatures(),
                             model="intrinsic") == expected


def test_gap_stress_bs_foreign_position_no_iv_uses_fallback_and_is_finite():
    # (b) A foreign spread (credit 0, no per-leg IV) still produces a FINITE stressed loss via the
    # injected iv_fn's fallback IV -- never None/NaN/raise.
    import math
    f = S2bFeatures()
    foreign = _ForeignSpread(572.0, 562.0, 0.0, 3, "2026-07-18")
    iv_fn = lambda p: (f.gap_fallback_iv, f.gap_fallback_iv)
    got = gap_stress_losses([foreign], 575.0, 6.0, 10.0, today="2026-07-09", iv_fn=iv_fn, f=f)
    for d in (1.0, 1.5, 2.0):
        assert math.isfinite(got[d])
    assert got[1.5] > 0    # a foreign short 572 put is stressed into the money at -1.5 ATR


def test_orchestrator_bs_gap_stress_logged_equals_enforced_and_exceeds_intrinsic():
    # (c) logged == enforced under BS: the §12 DECISION agg_gap_stress_1_5 equals the §10 OPEN
    # gap_stress_1_5 on the BS path (all three call sites share one iv_fn/today/features), AND the BS
    # loss exceeds the intrinsic loss for the same scenario (proving BS is actually the active model).
    # Advisor Step 2A: the orchestrator no longer has an intrinsic ENFORCEMENT mode to select -- BS is
    # unconditional -- so the comparison arm now calls gap_stress_losses(model="intrinsic") directly,
    # which is exactly what the Step 2B comparison logging does.
    log = []
    f = S2bFeatures(aggregate_risk_budget=True, decision_logging=True, max_gap_stress_loss_pct=1.0)
    d = _deps(features=f, trade_log=lambda rec: log.append(rec))
    state, info = run_entry_cycle(BotState(), d, MONDAY)
    assert info == "filled"
    decision = next(r for r in log if r["event"] == "DECISION" and r["decision"] == "filled")
    open_rec = next(r for r in log if r["event"] == "OPEN")
    assert decision["agg_gap_stress_1_5"] == pytest.approx(open_rec["gap_stress_1_5"])
    bs_loss = open_rec["gap_stress_1_5"]

    opened = state.open_positions[-1]
    intrinsic_loss = gap_stress_losses([opened], 575.0, 6.0, 10.0, model="intrinsic")[1.5]
    assert bs_loss > intrinsic_loss


# ── iv_fn IV-source priority: real greeks -> VIX-derived -> fallback (item 5 A + nit B3) ─

def test_iv_fn_vix_derived_and_fallback_sources():
    # nit B3: direct iv_fn unit test. No greeks dep wired -> the VIX-derived / fallback branch only.
    d = _deps(features=S2bFeatures())   # gap_fallback_iv defaults 0.20
    pos = _pos(743.0, 733.0, 1.39, 8)
    _, iv_fn = _make_gap_iv_resolver(d, None)                                   # regime=None
    assert iv_fn(pos) == (0.20, 0.20)                                           # -> gap_fallback_iv
    _, iv_fn = _make_gap_iv_resolver(d, types.SimpleNamespace(vix_level=16.0))  # real VIX 16
    assert iv_fn(pos) == (0.16, 0.16)                                           # -> 16/100
    _, iv_fn = _make_gap_iv_resolver(d, types.SimpleNamespace(vix_level=0))     # VIX 0 (bad)
    assert iv_fn(pos) == (0.20, 0.20)                                           # -> gap_fallback_iv


def test_iv_fn_real_greeks_per_leg_with_fallback():
    # A6(a)+(b): real per-leg mid_iv from the batched fetch is preferred; a leg missing from the
    # fetch falls back (here to VIX-derived, since a positive vix_level is supplied).
    pos = _pos(743.0, 733.0, 1.39, 8)   # _pos expiry 2026-07-18
    short_sym = _occ("SPY", "2026-07-18", "P", 743.0)
    fetch = lambda syms: {short_sym: 0.31}          # only the SHORT leg has real greeks
    d = _deps(features=S2bFeatures(), option_greeks_iv=fetch)
    prime, iv_fn = _make_gap_iv_resolver(d, types.SimpleNamespace(vix_level=16.0))
    prime([pos])
    assert iv_fn(pos) == (0.31, 0.16)               # short=real mid_iv, long=VIX-derived fallback


def test_orchestrator_greeks_fetch_batched_and_memoized_single_call():
    # A6(c) batched + A6(d) memoized: a full Bot-B cycle issues EXACTLY ONE greeks fetch, and that
    # single call carries every unique leg symbol in the gap-stress book (proposed + foreign).
    calls = []
    def fetch(syms):
        calls.append(list(syms))
        return {s: 0.19 for s in syms}
    foreign = ManagedPosition("SPY", 572.0, 562.0, credit=0.0, qty=3, expiry="2026-06-19",
                               entry_date="2026-06-10")
    log = []
    state = BotState()
    f = S2bFeatures(aggregate_risk_budget=True, decision_logging=True, max_gap_stress_loss_pct=1.0)
    d = _deps(features=f, trade_log=lambda rec: log.append(rec),
              account_spy_spreads=lambda: [foreign], option_greeks_iv=fetch)
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    assert len(calls) == 1                                # one fetch shared across all 3 sites
    got = set(calls[0])
    for strike in (568.0, 558.0, 572.0, 562.0):          # proposed 568/558 + foreign 572/562
        assert _occ("SPY", "2026-06-19", "P", strike) in got


def test_orchestrator_bot_c_triggers_zero_greeks_fetch():
    # A6(e) GUARDRAIL: both gates OFF (live Bot C) -> gap-stress paths never run -> ZERO greeks calls.
    calls = []
    fetch = lambda syms: (calls.append(list(syms)) or {})
    state = BotState()
    d = _deps(option_greeks_iv=fetch)    # default features -> aggregate_risk_budget & decision_logging OFF
    assert d.features.aggregate_risk_budget is False and d.features.decision_logging is False
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    assert calls == []                                   # Bot C fetched nothing (byte-identical)


def test_orchestrator_logged_equals_enforced_under_real_greeks():
    # A6(f): with real per-leg mid_iv, the §12 DECISION agg_gap_stress_1_5 still == the §10 OPEN
    # gap_stress_1_5 (all three sites share the one memoized fetch + iv_fn).
    fetch = lambda syms: {s: 0.22 for s in syms}
    foreign = ManagedPosition("SPY", 572.0, 562.0, credit=0.0, qty=3, expiry="2026-06-19",
                               entry_date="2026-06-10")
    log = []
    state = BotState()
    f = S2bFeatures(aggregate_risk_budget=True, decision_logging=True, max_gap_stress_loss_pct=1.0)
    d = _deps(features=f, trade_log=lambda rec: log.append(rec),
              account_spy_spreads=lambda: [foreign], option_greeks_iv=fetch)
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    decision = next(r for r in log if r["event"] == "DECISION" and r["decision"] == "filled")
    open_rec = next(r for r in log if r["event"] == "OPEN")
    assert decision["agg_gap_stress_1_5"] == pytest.approx(open_rec["gap_stress_1_5"])
    assert open_rec["gap_stress_1_5"] != 0.0
