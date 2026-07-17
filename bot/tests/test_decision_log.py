"""TDD for Task 1.7: exposure telemetry + decision logging (partner review v2 §12, §1).

Spec §1: "Every decision and feature-flag state must be written to the trade log." Behind
deps.features.decision_logging (opt-in, default OFF -> Bot C / run_s2b_live.py unaffected).
"""
import csv as _csv
from datetime import datetime

from bot.app.orchestrator import BotState, Deps, run_entry_cycle
from bot.strategy.manage import ManagedPosition
from bot.strategy.s2b import OptionQuote
from bot.risk_gate import AccountState
from bot.features import S2bFeatures


def _pos(short, long_, credit, qty, expiry="2026-06-19", entry_date="2026-06-15"):
    return ManagedPosition("SPY", short, long_, credit, qty, expiry, entry_date=entry_date)


def _chain(short_bid=4.00, long_ask=2.00):
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
TUESDAY = datetime(2026, 6, 16, 10, 5)


def _decisions(logged):
    return [r for r in logged if r.get("event") == "DECISION"]


# ── (a) decision_logging=True: a reject writes a DECISION record with reason + flags ────────────

def test_reject_writes_decision_record_with_reason_and_flags():
    logged = []
    f = S2bFeatures(decision_logging=True, aggregate_risk_budget=True)
    d = _deps(features=f, trade_log=lambda rec: logged.append(rec))
    state = BotState()
    state, info = run_entry_cycle(state, d, TUESDAY)   # wrong weekday -> rejected
    assert info is None
    decisions = _decisions(logged)
    assert len(decisions) == 1
    rec = decisions[0]
    assert rec["decision"] == "wrong_weekday"
    assert rec["date"] == "2026-06-16"
    assert rec["flags"]["aggregate_risk_budget"] is True
    assert rec["flags"]["credit_tiers"] is False


def test_credit_too_low_reject_writes_decision_record():
    logged = []
    f = S2bFeatures(decision_logging=True, credit_tiers=True)
    d = _deps(features=f, trade_log=lambda rec: logged.append(rec),
              get_chain=lambda sym, exp: _chain(3.00, 2.10))  # natural=0.90 -> exec_credit=0.97, ratio=0.097
    state = BotState()
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "credit_too_low"
    decisions = _decisions(logged)
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "credit_too_low"
    # by this point in the cycle, expiry/order are known -> telemetry has real values
    assert decisions[0]["positions_in_expiry"] == 0
    assert decisions[0]["agg_remaining_stop"] == 0.0
    assert decisions[0]["agg_structural"] == 0.0


def test_risk_budget_reject_writes_decision_record():
    logged = []
    f = S2bFeatures(decision_logging=True, aggregate_risk_budget=True)
    d = _deps(features=f, trade_log=lambda rec: logged.append(rec),
              risk_equity=lambda: 1.0)   # tiny budget -> size_qty rounds to 0
    state = BotState()
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "risk_budget"
    decisions = _decisions(logged)
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "risk_budget"


def test_duplicate_strikes_reject_writes_decision_record():
    logged = []
    pos = _pos(568.0, 558.0, 3.0, 1)
    f = S2bFeatures(decision_logging=True)
    d = _deps(features=f, trade_log=lambda rec: logged.append(rec), max_open=3)
    state = BotState(open_positions=[pos])
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "duplicate_strikes"
    decisions = _decisions(logged)
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "duplicate_strikes"
    assert decisions[0]["positions_in_expiry"] == 1


# ── (b) a filled entry ALSO writes a DECISION record ─────────────────────────────────────────────

def test_filled_entry_writes_decision_record():
    logged = []
    f = S2bFeatures(decision_logging=True)
    d = _deps(features=f, trade_log=lambda rec: logged.append(rec))
    state = BotState()
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    events = [r["event"] for r in logged]
    assert "OPEN" in events and "DECISION" in events
    decisions = _decisions(logged)
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "filled"


# ── (c) exposure telemetry aggregates over the open book ────────────────────────────────────────

def test_telemetry_aggregates_over_open_book():
    logged = []
    p_today = _pos(540.0, 530.0, 2.0, 3, expiry="2026-06-19", entry_date="2026-06-15")
    p_prior = _pos(520.0, 510.0, 1.5, 2, expiry="2026-07-17", entry_date="2026-06-01")
    f = S2bFeatures(decision_logging=True)
    d = _deps(features=f, trade_log=lambda rec: logged.append(rec),
              max_open=3, mark_position=lambda p: 1.0,
              # a different (non-duplicate) chain so the entry attempt proceeds past dedup
              get_chain=lambda sym, exp: _chain(4.00, 2.00))
    state = BotState(open_positions=[p_today, p_prior])
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    decisions = _decisions(logged)
    rec = decisions[-1]   # the "filled" DECISION record, computed over the full book incl. proposed
    assert rec["positions_today"] == 1   # only p_today matches entry_date == today
    assert rec["positions_in_expiry"] == 1   # only p_today shares the 2026-06-19 expiry
    # adjacent_strike_distance: same-expiry short strikes are 540.0 (p_today) and 568.0 (proposed)
    assert rec["adjacent_strike_distance"] == 28.0
    # agg_remaining_stop / agg_structural must be > 0 (both open positions contribute)
    assert rec["agg_remaining_stop"] > 0
    assert rec["agg_structural"] > 0


# ── (d) decision_logging=False (Bot C default): NO DECISION records, field set unchanged ────────

def test_decision_logging_off_writes_no_decision_records():
    logged = []
    d = _deps(trade_log=lambda rec: logged.append(rec))   # default S2bFeatures() -> decision_logging False
    assert d.features.decision_logging is False
    state = BotState()
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "filled"
    assert _decisions(logged) == []


def test_decision_logging_off_on_reject_writes_no_decision_records():
    logged = []
    d = _deps(trade_log=lambda rec: logged.append(rec))
    state = BotState()
    state, info = run_entry_cycle(state, d, TUESDAY)
    assert _decisions(logged) == []


def test_s2b_features_decision_logging_default_off():
    assert S2bFeatures().decision_logging is False


def test_alldays_features_have_decision_logging_on():
    from bot.app.run_s2b_alldays import ALLDAYS_FEATURES
    assert ALLDAYS_FEATURES.decision_logging is True


def test_run_s2b_live_enables_decision_logging():
    # decision_logging enabled on the live bot 2026-07-16 (LIVE_FEATURES); default stays off.
    from bot.app.run_s2b_live import LIVE_FEATURES
    assert LIVE_FEATURES.decision_logging is True
    assert S2bFeatures().decision_logging is False


# ── trade-log field set: decision columns only added when include_decision_columns=True ─────────

def test_make_trade_logger_default_has_no_decision_columns(tmp_path):
    from bot.app.wiring import make_trade_logger
    p = tmp_path / "trades_live.csv"
    log = make_trade_logger(str(p))
    log({"event": "OPEN", "date": "2026-06-15", "ticker": "SPY", "short": 568.0,
         "long": 558.0, "qty": 2, "credit": 1.7, "status": "filled",
         "decision": "filled", "flags": {"credit_tiers": False}})
    header = open(str(p)).readline().strip().split(",")
    assert "decision" not in header and "flags" not in header


def test_make_trade_logger_include_decision_columns_true_adds_decision_fields(tmp_path):
    from bot.app.wiring import make_trade_logger
    p = tmp_path / "trades_alldays.csv"
    log = make_trade_logger(str(p), include_decision_columns=True)
    log({"event": "DECISION", "date": "2026-06-15", "decision": "filled",
         "flags": {"credit_tiers": False}, "positions_today": 1, "positions_in_expiry": 1,
         "adjacent_strike_distance": 10.0, "agg_remaining_stop": 100.0,
         "agg_structural": 200.0, "agg_gap_stress_1_5": 50.0})
    header = open(str(p)).readline().strip().split(",")
    for col in ("decision", "flags", "positions_today", "positions_in_expiry",
                "adjacent_strike_distance", "agg_remaining_stop", "agg_structural",
                "agg_gap_stress_1_5"):
        assert col in header


def test_build_and_run_gates_include_decision_columns_on_decision_logging_flag():
    import inspect
    from bot.app import run_s2b
    src = inspect.getsource(run_s2b.build_and_run)
    assert "include_decision_columns=resolved_features.decision_logging" in src


# ── Bug fix: credit-tier (§3) + cost-gate (§5) + exec-credit (§4) telemetry must survive the
# DictWriter's extrasaction="ignore" too -- they were being silently dropped from
# trades_alldays.csv because _DECISION_LOG_FIELDS never listed them. ──────────────────────────────

def test_make_trade_logger_include_decision_columns_true_adds_credit_and_cost_telemetry(tmp_path):
    from bot.app.wiring import make_trade_logger
    p = tmp_path / "trades_alldays.csv"
    log = make_trade_logger(str(p), include_decision_columns=True)
    log({"event": "DECISION", "date": "2026-06-15", "decision": "filled",
         "flags": {"credit_tiers": True}, "positions_today": 1, "positions_in_expiry": 1,
         "adjacent_strike_distance": 10.0, "agg_remaining_stop": 100.0,
         "agg_structural": 200.0, "agg_gap_stress_1_5": 50.0,
         "expected_executable_credit": 1.77,
         "credit_ratio": 0.177, "credit_pctl40": 0.15, "credit_sample_count": 12,
         "credit_threshold": 0.20, "credit_quality_mult": 1.0,
         "cost_gross_target": 0.85, "cost_round_trip": 0.30, "cost_target_ratio": 2.83})
    rows = list(_csv.DictReader(open(str(p))))
    header = rows and list(rows[0].keys())
    row = rows[0]
    for col, expected in (
        ("expected_executable_credit", "1.77"),
        ("credit_ratio", "0.177"), ("credit_pctl40", "0.15"), ("credit_sample_count", "12"),
        ("credit_threshold", "0.2"), ("credit_quality_mult", "1.0"),
        ("cost_gross_target", "0.85"), ("cost_round_trip", "0.3"), ("cost_target_ratio", "2.83"),
    ):
        assert col in row, f"{col} missing from CSV header -- silently dropped by extrasaction=ignore"
        assert row[col] == expected, f"{col}: expected {expected!r}, got {row[col]!r}"


def test_make_trade_logger_default_still_has_original_field_set_only(tmp_path):
    # Bot C (decision_logging off) must keep exactly the original 12-column shape -- no new
    # credit/cost columns leak in even if a record happened to carry them (defensive; in practice
    # decision_logging off means no DECISION records are ever built at all).
    from bot.app.wiring import make_trade_logger
    p = tmp_path / "trades_live.csv"
    log = make_trade_logger(str(p))
    log({"event": "OPEN", "date": "2026-06-15", "ticker": "SPY", "short": 568.0,
         "long": 558.0, "qty": 2, "credit": 1.7, "status": "filled",
         "credit_ratio": 0.177, "cost_gross_target": 0.85})
    header = open(str(p)).readline().strip().split(",")
    assert header == ["event", "date", "ticker", "short", "long", "expiry", "qty",
                      "credit", "action", "exit_value", "pnl", "status"]


def test_risk_budget_bound_reject_writes_all_telemetry_to_csv(tmp_path):
    """Task 7 review regression: drive run_entry_cycle into a REAL budget-bound reject (expiry_stop
    binds) through the ACTUAL make_trade_logger CSV path (include_decision_columns=True), and assert
    the decision string AND all six risk_budget_* telemetry fields survive to the written CSV. This
    catches the extrasaction='ignore' drop bug -- the fields must be in wiring._DECISION_LOG_FIELDS.

    (test_risk_budget_reject_writes_decision_record uses risk_equity=1.0, where size_qty floors to 0
    BEFORE any budget is consulted -> limiting_budget is None -> bare 'risk_budget', so it never
    exercised this per-budget-attribution path at all.)"""
    from bot.app.wiring import make_trade_logger
    p = tmp_path / "trades_alldays.csv"
    trade_log = make_trade_logger(str(p), include_decision_columns=True)
    f = S2bFeatures(decision_logging=True, aggregate_risk_budget=True)
    # Book already heavy in the SAME expiry (2026-06-19) as the proposed trade, opened a PRIOR day
    # (so it does NOT touch the same-day budget) at UNRELATED strikes (so no duplicate_strikes):
    # remaining_stop_risk(2.0, mark=3.0, qty=9, slip=0.10) = (3*2.0-3.0+0.10)*100*9 = 3.10*100*9 =
    # 2790 -> expiry_stop=2790 vs expiry budget 100000*0.03=3000 -> headroom 210 < 410 per-contract
    # stop, so the proposed qty (size_qty -> 1) is capped to 0 by expiry_stop specifically. The
    # total-stop (4000) / same-day (2000, zero exposure) / structural (15000) budgets don't bind.
    book = [_pos(560.0, 550.0, 2.0, 9, expiry="2026-06-19", entry_date="2026-06-11")]
    d = _deps(features=f, trade_log=trade_log, max_open=5)
    state = BotState(open_positions=book)
    state, info = run_entry_cycle(state, d, MONDAY)
    assert info == "risk_budget"

    rows = list(_csv.DictReader(open(str(p))))
    decisions = [r for r in rows if r.get("event") == "DECISION"]
    assert len(decisions) == 1
    rec = decisions[0]
    # (a) the enriched decision string names the specific binding budget
    assert rec["decision"] == "risk_budget:expiry_stop"
    # (b) all six risk_budget_* fields survive to the CSV with correct values (CSV -> strings)
    for col, expected in (
        ("risk_budget_limiting", "expiry_stop"),
        ("risk_budget_exposure", "2790.0"),
        ("risk_budget_limit", "3000.0"),
        ("risk_budget_headroom", "210.0"),
        ("risk_budget_proposed_qty", "1"),
        ("risk_budget_permitted_qty", "0"),
    ):
        assert col in rec, f"{col} missing from CSV -- silently dropped by extrasaction=ignore"
        assert rec[col] == expected, f"{col}: expected {expected!r}, got {rec[col]!r}"
