"""TDD for Task 2.1: expected executable credit + quote-quality guards wired into the entry
cycle (partner review v2 §4). Active when deps.features.credit_tiers OR
deps.features.transaction_cost_gate is on (both need the conservative credit); a no-op --
byte-identical behavior -- when both are off (Bot C / run_s2b_live.py)."""
from datetime import datetime

from bot.app.orchestrator import BotState, Deps, run_entry_cycle
from bot.strategy.s2b import OptionQuote
from bot.risk_gate import AccountState
from bot.features import S2bFeatures


def _acct(today, conc):
    return AccountState(100_000.0, 100_000.0, 0.0, conc, 0.0, {}, today)


def _deps(**over):
    base = dict(
        get_spot=lambda sym: 575.0, get_atr=lambda sym: 6.0,
        get_chain=lambda sym, exp: _normal_chain(),
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


def _normal_chain():
    # short 568 bid/ask 3.40/3.50; long 558 bid/ask 1.60/1.70 -- tight, valid quotes.
    return [
        OptionQuote(strike=568.0, delta=0.36, bid=3.40, ask=3.50),
        OptionQuote(strike=558.0, delta=0.18, bid=1.60, ask=1.70),
    ]


def _crossed_chain():
    # short leg crossed: bid (3.50) > ask (3.40) -> quotes_valid() is False
    return [
        OptionQuote(strike=568.0, delta=0.36, bid=3.50, ask=3.40),
        OptionQuote(strike=558.0, delta=0.18, bid=1.00, ask=1.10),
    ]


def _wide_chain():
    # short 3.00/5.00 (mid 4.00), long 0.00/0.10 (mid 0.05) -> mid=3.95, natural=2.90,
    # width=1.05, ratio=0.2658 > 0.25 default max_package_width_ratio -> too wide
    return [
        OptionQuote(strike=568.0, delta=0.36, bid=3.00, ask=5.00),
        OptionQuote(strike=558.0, delta=0.18, bid=0.00, ask=0.10),
    ]


MONDAY = datetime(2026, 6, 15, 10, 5)   # Monday 10:05, expiry 2026-06-19


def _decisions(logged):
    return [r for r in logged if r.get("event") == "DECISION"]


# ── credit_tiers ON: crossed/invalid quotes rejected ─────────────────────────────────────────────

def test_credit_tiers_on_rejects_crossed_quotes():
    logged = []
    f = S2bFeatures(credit_tiers=True, decision_logging=True)
    d = _deps(features=f, get_chain=lambda sym, exp: _crossed_chain(),
              trade_log=lambda rec: logged.append(rec))
    state = BotState()
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "quote_invalid"
    assert state.open_positions == []
    decisions = _decisions(logged)
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "quote_invalid"


def test_credit_tiers_on_rejects_wide_package():
    logged = []
    f = S2bFeatures(credit_tiers=True, decision_logging=True)
    d = _deps(features=f, get_chain=lambda sym, exp: _wide_chain(),
              trade_log=lambda rec: logged.append(rec))
    state = BotState()
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "quote_wide"
    assert state.open_positions == []
    decisions = _decisions(logged)
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "quote_wide"


# ── transaction_cost_gate ON alone also triggers the guards (either flag suffices) ───────────────

def test_transaction_cost_gate_on_alone_rejects_wide_package():
    f = S2bFeatures(transaction_cost_gate=True)
    d = _deps(features=f, get_chain=lambda sym, exp: _wide_chain())
    state = BotState()
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "quote_wide"


# ── credit_tiers ON with clean quotes: passes the guard, fills, and logs the expected credit ─────

def test_credit_tiers_on_clean_quotes_fills_and_logs_expected_credit():
    logged = []
    f = S2bFeatures(credit_tiers=True, decision_logging=True)
    d = _deps(features=f, trade_log=lambda rec: logged.append(rec))
    state = BotState()
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    assert len(state.open_positions) == 1
    decisions = _decisions(logged)
    filled_decision = [r for r in decisions if r["decision"] == "filled"][0]
    # package_mid=1.80, package_natural=1.70, slip=0.03 -> mid-slip=1.77 -> max(1.70,1.77)=1.77
    assert filled_decision["expected_executable_credit"] == 1.77


# ── both flags OFF (Bot C / run_s2b_live.py): guard never runs, wide/crossed quotes don't block ──

def test_flags_off_wide_package_not_rejected_unchanged_behavior():
    d = _deps(get_chain=lambda sym, exp: _wide_chain())   # default S2bFeatures(): both flags False
    assert d.features.credit_tiers is False and d.features.transaction_cost_gate is False
    state = BotState()
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    assert len(state.open_positions) == 1


def test_flags_off_crossed_quotes_not_rejected_unchanged_behavior():
    d = _deps(get_chain=lambda sym, exp: _crossed_chain())
    state = BotState()
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    assert len(state.open_positions) == 1


def test_flags_off_no_expected_executable_credit_logged():
    logged = []
    d = _deps(trade_log=lambda rec: logged.append(rec))
    state = BotState()
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    # decision_logging is off by default too, so there should be no DECISION records at all
    assert _decisions(logged) == []
