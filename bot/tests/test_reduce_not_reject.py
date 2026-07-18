"""Task 5 (post-v2 refinement §2/§3/§8/§10/§11): the aggregate_risk_budget entry pipeline REDUCES an
oversized request to fit the strictest risk cap and PROCEEDS, rather than rejecting outright.

Sizing sequence (spec §2 "Correct sequence"):
    base_qty (size_qty, quality_multiplier=1.0)
      -> quality_qty (apply_quality_multiplier, folds in the credit-quality multiplier + low-credit max)
      -> size_to_risk_limits(quality_qty, budget caps + gap caps)  # reduce-not-reject
Only final_qty <= 0 rejects. Every cap-bound reject returns the bare "risk_budget" info string; the
SPECIFIC binding gate lives in the decision-log `limiting_gate` (and the logged reason
f"risk_budget:{limiting_gate}"). Gap enforcement is BS-only (spec §9). All INSIDE
`if deps.features.aggregate_risk_budget:` -- the aggregate-OFF path stays byte-identical.
"""
from datetime import datetime

from bot.app.orchestrator import BotState, Deps, run_entry_cycle
from bot.strategy.manage import ManagedPosition
from bot.strategy.s2b import OptionQuote
from bot.risk_gate import AccountState
from bot.features import S2bFeatures


def _pos(short, long_, credit, qty, expiry="2026-06-19", entry_date="2026-06-15"):
    return ManagedPosition("SPY", short, long_, credit, qty, expiry, entry_date=entry_date)


def _chain(short_bid=4.00, long_ask=2.00):
    # SPY 568/558 (spot=575, atr=6.0). credit = short_bid - long_ask; default 4.00/2.00 -> credit 2.00.
    return [
        OptionQuote(568.0, 0.36, short_bid, short_bid + 0.10),
        OptionQuote(558.0, 0.18, long_ask - 0.10, long_ask),
    ]


def _acct(today, conc):
    return AccountState(100_000.0, 100_000.0, 0.0, conc, 0.0, {}, today)


def _deps(**over):
    base = dict(
        get_spot=lambda sym: 575.0, get_atr=lambda sym: 6.0,
        get_chain=lambda sym, exp: _chain(),
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


def _filled_decision(logged):
    return next(r for r in logged if r.get("event") == "DECISION" and r["decision"] == "filled")


# ── (a) reduce-not-reject: an oversized quality qty is SIZED DOWN to the binding cap and opened ──

def test_oversized_candidate_reduced_not_rejected():
    # req=1,000,000, credit=2.0 -> base_qty = size_qty(quality_multiplier=1.0):
    #   per-contract stop = planned_stop_loss_per_contract(2.0, 10, 0.10) = min(800, 410) = 410
    #   qty_entry = floor(1e6*0.0075/410) = floor(7500/410) = 18  (structural = floor(50000/800)=62)
    #   base = 18. credit_tiers OFF -> quality_multiplier 1.0 -> quality_qty = 18.
    # A prior-day book position in the SAME expiry consumes the expiry-stop budget so it caps at 5:
    #   expiry-stop exposure = remaining_stop_risk(2.0, mark=3.0, 89, 0.10) = 3.1*100*89 = 27590
    #   limit = 1e6*0.03 = 30000 -> remaining 2410 -> floor(2410/410) = 5. Far-OTM 545/535 strikes so
    #   gap stress stays loose; same-day (0 exposure)/total-stop/structural caps all exceed 5.
    # -> 18 REDUCED to 5 and OPENED (not rejected); expiry_stop is the binding gate.
    book = [_pos(545.0, 535.0, 2.0, 89, expiry="2026-06-19", entry_date="2026-06-11")]
    logged = []
    f = S2bFeatures(aggregate_risk_budget=True, decision_logging=True)
    d = _deps(features=f, trade_log=lambda r: logged.append(r), max_open=5,
              risk_equity=lambda: 1_000_000.0)
    state = BotState(open_positions=book)
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    new = [p for p in state.open_positions if (p.short_strike, p.long_strike) == (568.0, 558.0)]
    assert len(new) == 1
    assert new[0].qty == 5
    dec = _filled_decision(logged)
    assert dec["decision_outcome"] == "allowed_reduced"
    assert dec["limiting_gate"] == "expiry_stop"
    assert dec["requested_qty"] == 18
    assert dec["quality_adjusted_qty"] == 18
    assert dec["final_qty"] == 5
    assert dec["risk_current_exposure"] == 27590.0
    assert dec["risk_limit"] == 30000.0
    assert dec["risk_remaining_capacity"] == 2410.0
    assert dec["risk_incremental_per_contract"] == 410.0


# ── (b) still-rejects: a genuinely full book (a cap at 0) rejects at zero capacity ──────────────

def test_full_book_rejects_at_zero_capacity():
    # req=100k default -> base/quality qty = 1. A prior-day 560/550 qty9 position in the same expiry
    # drives expiry-stop exposure to 2790 vs limit 3000 (remaining 210 < 410 incremental) -> cap 0.
    book = [_pos(560.0, 550.0, 2.0, 9, expiry="2026-06-19", entry_date="2026-06-11")]
    logged = []
    f = S2bFeatures(aggregate_risk_budget=True, decision_logging=True)
    d = _deps(features=f, trade_log=lambda r: logged.append(r), max_open=5)
    state = BotState(open_positions=book)
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "risk_budget"                                  # bare info string (not the gate)
    assert not any((p.short_strike, p.long_strike) == (568.0, 558.0) for p in state.open_positions)
    dec = next(r for r in logged if r.get("event") == "DECISION")
    assert dec["decision"] == "risk_budget:expiry_stop"           # the gate lives in the LOGGED reason
    assert dec["decision_outcome"] == "blocked_zero_capacity"
    assert dec["limiting_gate"] == "expiry_stop"
    assert dec["requested_qty"] == 1
    assert dec["final_qty"] == 0


# ── (c) allowed_full: a small candidate that fits every cap opens at the requested quantity ──────

def test_small_candidate_allowed_full():
    logged = []
    f = S2bFeatures(aggregate_risk_budget=True, decision_logging=True)
    d = _deps(features=f, trade_log=lambda r: logged.append(r))   # 100k, empty book -> base qty 1
    state = BotState()
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    assert state.open_positions[0].qty == 1
    dec = _filled_decision(logged)
    assert dec["decision_outcome"] == "allowed_full"
    assert dec["final_qty"] == dec["requested_qty"] == 1
    assert dec["limiting_gate"] is not None                       # tightest cap still named


# ── (d) Bot-C-safe: aggregate_risk_budget OFF -> legacy sizing, no risk-sizing telemetry emitted ──

def test_aggregate_off_path_byte_identical_and_no_risk_sizing_telemetry():
    logged = []
    d = _deps(features=S2bFeatures(decision_logging=True), trade_log=lambda r: logged.append(r))
    assert d.features.aggregate_risk_budget is False
    state = BotState()
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    # legacy contracts_for_risk sizing (credit 2.0, max_loss 800, 100k) -> 12, untouched by any cap
    assert state.open_positions[0].qty == 12
    dec = _filled_decision(logged)
    assert "limiting_gate" not in dec        # risk-sizing telemetry is NEVER set on the aggregate-OFF path
    assert "decision_outcome" not in dec
