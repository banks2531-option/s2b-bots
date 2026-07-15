"""Adaptive credit-tier sizing wired into the entry cycle (partner review §2). OPT-IN via
deps.min_credit_ratio (0.0 = feature OFF, byte-identical to prior behavior)."""
from datetime import datetime

from bot.app.orchestrator import BotState, Deps, run_entry_cycle
from bot.strategy.s2b import OptionQuote
from bot.risk_gate import AccountState


def _custom_chain(short_bid, long_ask):
    """Minimal 568/558 SPY chain (spot=575, atr=6.0) with a controlled credit = short_bid - long_ask."""
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


# ── (a) below min_credit_ratio -> rejected, nothing opened ─────────────────────

def test_credit_ratio_below_min_rejects_entry():
    state = BotState()
    d = _deps(get_chain=lambda sym, exp: _custom_chain(3.00, 2.10),  # credit=0.90, ratio=0.09
              min_credit_ratio=0.10)
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "credit_too_low"
    assert state.open_positions == []


# ── (b) just above min but below full threshold -> probe (reduced) size ────────

def test_credit_ratio_probe_size_reduced_qty():
    state = BotState()
    d = _deps(get_chain=lambda sym, exp: _custom_chain(3.20, 2.10),  # credit=1.10, ratio=0.11
              min_credit_ratio=0.10)
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    assert len(state.open_positions) == 1
    # base sizing (contracts_for_risk on 20k equity, max_loss=(10-1.10)*100=890) would be qty=2;
    # probe_size_multiplier defaults to 0.40 -> max(1, int(2*0.40)) == 1
    assert state.open_positions[0].qty == 1


# ── (c) well above full threshold -> full size (unchanged from base sizing) ────

def test_credit_ratio_full_size_when_above_threshold():
    state = BotState()
    d = _deps(get_chain=lambda sym, exp: _custom_chain(4.00, 2.00),  # credit=2.00, ratio=0.20
              min_credit_ratio=0.10)
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    assert len(state.open_positions) == 1
    # base sizing: max_loss=(10-2.00)*100=800 -> floor(2000/800)=2; ratio 0.20 clears the floor
    # threshold (0.115) -> full size, no reduction
    assert state.open_positions[0].qty == 2


# ── (d) credit_ratio_history grows in the right DTE bucket after a fill ────────

def test_credit_ratio_history_grows_on_fill():
    state = BotState()
    d = _deps(get_chain=lambda sym, exp: _custom_chain(4.00, 2.00),  # credit=2.00, ratio=0.20
              min_credit_ratio=0.10)
    assert state.credit_ratio_history == {}
    state, info = run_entry_cycle(state, d, MONDAY)   # Monday 10:05, expiry 2026-06-19 -> dte=4
    assert info == "filled"
    assert state.credit_ratio_history.get("4-5") == [0.2]


def test_credit_ratio_history_not_recorded_on_reject():
    state = BotState()
    d = _deps(get_chain=lambda sym, exp: _custom_chain(3.00, 2.10),  # credit=0.90, ratio=0.09 -> reject
              min_credit_ratio=0.10)
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "credit_too_low"
    assert state.credit_ratio_history == {}


# ── (e) feature OFF by default (min_credit_ratio=0.0) -> byte-identical behavior ──

def test_credit_ratio_feature_off_by_default_no_gating():
    # a ratio that WOULD be rejected under min_credit_ratio=0.10 must open normally when the
    # feature is off (the default), at unreduced qty, with no history recorded.
    state = BotState()
    d = _deps(get_chain=lambda sym, exp: _custom_chain(3.00, 2.10))  # credit=0.90, ratio=0.09
    assert d.min_credit_ratio == 0.0
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    assert len(state.open_positions) == 1
    # base sizing: max_loss=(10-0.90)*100=910 -> floor(2000/910)=2, unreduced
    assert state.open_positions[0].qty == 2
    assert state.credit_ratio_history == {}


def test_deps_credit_defaults_are_off():
    d = _deps()
    assert d.min_credit_ratio == 0.0
    assert d.credit_floor_ratio == 0.115
    assert d.probe_size_multiplier == 0.40
    assert d.use_adaptive_credit is False


def test_botstate_credit_ratio_history_default_empty_dict():
    s = BotState()
    assert s.credit_ratio_history == {}
