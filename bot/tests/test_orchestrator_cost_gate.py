"""TDD for Task 2.3: transaction-cost profitability gate wired into the entry cycle (partner
review v2 §5). Runs AFTER the credit-tier decision (credit_too_low, if any, is resolved first) and
BEFORE sizing. OPT-IN via deps.features.transaction_cost_gate (False = feature OFF, byte-identical
to prior behavior)."""
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
        get_chain=lambda sym, exp: _rich_chain(),
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


def _rich_chain():
    # short 568 bid/ask 3.40/3.50; long 558 bid/ask 1.60/1.70. natural=1.70, mid=1.80, mid-slip=1.77
    # -> exec_credit=1.77 -> gross_target=88.5; rt_cost=7.30; ratio=12.123 -> comfortably passes.
    return [
        OptionQuote(strike=568.0, delta=0.36, bid=3.40, ask=3.50),
        OptionQuote(strike=558.0, delta=0.18, bid=1.60, ask=1.70),
    ]


def _thin_chain():
    # short 568 bid/ask 0.50/0.55; long 558 bid/ask 0.10/0.15. natural=0.35, mid=0.40, mid-slip=0.37
    # -> exec_credit=0.37 -> gross_target=18.5; rt_cost=7.30; ratio=2.534 -> fails the 4.0x gate.
    return [
        OptionQuote(strike=568.0, delta=0.36, bid=0.50, ask=0.55),
        OptionQuote(strike=558.0, delta=0.18, bid=0.10, ask=0.15),
    ]


def _borderline_chain():
    # short 568 bid/ask 0.60/0.65; long 558 bid/ask 0.10/0.15. natural=0.45, mid=0.50, mid-slip=0.47
    # -> exec_credit=0.47 -> gross_target=23.5; rt_cost=7.30; ratio=3.219 -> fails at 4.0x, passes at 3.0x.
    return [
        OptionQuote(strike=568.0, delta=0.36, bid=0.60, ask=0.65),
        OptionQuote(strike=558.0, delta=0.18, bid=0.10, ask=0.15),
    ]


MONDAY = datetime(2026, 6, 15, 10, 5)   # Monday 10:05, expiry 2026-06-19


def _decisions(logged):
    return [r for r in logged if r.get("event") == "DECISION"]


# ── flag ON: thin credit rejected "cost_gate", nothing opened, components logged ────────────────

def test_cost_gate_rejects_thin_target_and_logs_components():
    logged = []
    f = S2bFeatures(transaction_cost_gate=True, decision_logging=True)
    d = _deps(features=f, get_chain=lambda sym, exp: _thin_chain(),
              trade_log=lambda rec: logged.append(rec))
    state = BotState()
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "cost_gate"
    assert state.open_positions == []
    decisions = _decisions(logged)
    assert len(decisions) == 1
    rec = decisions[0]
    assert rec["decision"] == "cost_gate"
    assert rec["cost_gross_target"] == 18.5
    assert rec["cost_round_trip"] == 7.30
    assert rec["cost_target_ratio"] == 2.534
    assert rec["expected_executable_credit"] == 0.37


# ── flag ON: rich credit passes the gate and fills ───────────────────────────────────────────────

def test_cost_gate_passes_rich_credit_and_fills():
    logged = []
    f = S2bFeatures(transaction_cost_gate=True, decision_logging=True)
    d = _deps(features=f, trade_log=lambda rec: logged.append(rec))   # default _rich_chain
    state = BotState()
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    assert len(state.open_positions) == 1
    decisions = _decisions(logged)
    filled = [r for r in decisions if r["decision"] == "filled"][0]
    assert filled["cost_gross_target"] == 88.5
    assert filled["cost_round_trip"] == 7.30
    assert filled["cost_target_ratio"] == 12.123


# ── ratio configurable: same borderline candidate rejected at 4.0x, passes at 3.0x ───────────────

def test_cost_gate_ratio_configurable_rejects_at_default_4x():
    f = S2bFeatures(transaction_cost_gate=True)
    d = _deps(features=f, get_chain=lambda sym, exp: _borderline_chain())
    state = BotState()
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "cost_gate"
    assert state.open_positions == []


def test_cost_gate_ratio_configurable_passes_at_3x():
    f = S2bFeatures(transaction_cost_gate=True, min_target_to_cost_ratio=3.0)
    d = _deps(features=f, get_chain=lambda sym, exp: _borderline_chain())
    state = BotState()
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    assert len(state.open_positions) == 1


# ── ordering: credit_too_low (credit_tiers) is resolved BEFORE the cost gate ever runs ───────────

def test_cost_gate_does_not_run_when_credit_tiers_rejects_first():
    logged = []
    f = S2bFeatures(credit_tiers=True, transaction_cost_gate=True, decision_logging=True)
    # exec_credit=0.37/wing(10)=0.037 < min_credit_ratio(0.10) -> credit_too_low, before cost gate
    d = _deps(features=f, get_chain=lambda sym, exp: _thin_chain(),
              trade_log=lambda rec: logged.append(rec))
    state = BotState()
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "credit_too_low"
    decisions = _decisions(logged)
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "credit_too_low"
    assert "cost_gross_target" not in decisions[0]


# ── flag OFF (Bot C / run_s2b_live.py): gate never runs, thin credit fills unchanged ─────────────

def test_flag_off_thin_credit_not_rejected_unchanged_behavior():
    logged = []
    d = _deps(get_chain=lambda sym, exp: _thin_chain(), trade_log=lambda rec: logged.append(rec))
    assert d.features.transaction_cost_gate is False
    state = BotState()
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    assert len(state.open_positions) == 1
    # decision_logging is off by default too -> no DECISION records, and certainly no cost telemetry
    assert _decisions(logged) == []
