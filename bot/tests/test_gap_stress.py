"""Gap-risk stress test (partner review v2 §10): reprice spreads at SPY down 1/1.5/2 ATR."""
import pytest

from bot.portfolio.gap_stress import stressed_spread_loss, gap_stress_losses, gap_stress_ok
from bot.strategy.manage import ManagedPosition


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


# ── gap_stress_losses: sums across positions, for all three drops ──────────────

def _pos(short, long_, credit, qty):
    return ManagedPosition("SPY", short, long_, credit, qty, "2026-07-18", entry_date="2026-07-10")


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

from bot.app.orchestrator import BotState, Deps, run_entry_cycle
from bot.strategy.s2b import OptionQuote
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
    # credit=0.20, size_qty budgets to 5 (structural-bound; see risk_budget tests' math with a
    # smaller credit). A very tight gap-stress budget (0.005 * 100_000 = 500) forces a reduction:
    # per-contract 1.5-ATR loss = (2.0-0.2)*100 = 180 -> qty=2 fits (360 <= 500), qty=3 does not (540 > 500).
    log = []
    state = BotState()
    f = S2bFeatures(aggregate_risk_budget=True, max_gap_stress_loss_pct=0.005)
    d = _deps(features=f, trade_log=lambda rec: log.append(rec))
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    assert len(state.open_positions) == 1
    assert state.open_positions[0].qty == 2
    rec = log[-1]
    assert rec["event"] == "OPEN"
    assert rec["gap_stress_1_0"] == pytest.approx(-40.0)
    assert rec["gap_stress_1_5"] == pytest.approx(360.0)
    assert rec["gap_stress_2_0"] == pytest.approx(960.0)


def test_orchestrator_gap_stress_rejects_when_even_one_contract_fails():
    state = BotState()
    f = S2bFeatures(aggregate_risk_budget=True, max_gap_stress_loss_pct=0.001)
    d = _deps(features=f)
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "gap_stress"
    assert state.open_positions == []


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
