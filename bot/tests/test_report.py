"""TDD for Task R.1: daily/weekly report generator (partner spec §18). READ-ONLY -- this module
never touches the live bot or a trading decision; it only reads a CSV log a bot already wrote.

Fixture: two trading days across a mix of OPEN/CLOSE/DECISION/SHADOW rows (spec §18's four event
types). Field values below are hand-picked so every aggregate below has one clean, hand-computable
expected answer -- see the inline arithmetic next to each assertion.

Day 1 (Monday 2026-06-15, expiry 2026-06-19, dte=4 -> bucket "4-5"):
  DECISION credit_too_low (reject, candidate)
  DECISION filled, credit_quality_mult=1.0 (full), expected_executable_credit=1.65
  OPEN     568/558 qty=2 credit=1.70  (entry #1 of the day; tier=full; slippage=+0.05)
  DECISION filled, credit_quality_mult=0.40 (probe), expected_executable_credit=1.10
  OPEN     540/530 qty=1 credit=1.15  (entry #2 of the day; tier=probe; slippage=+0.05)
  CLOSE    568/558 same day, pnl=40.0 only (no gross/net -- tests the pnl fallback)
  SHADOW   shadow_score=0.42, shadow_action=half_size

Day 2 (Tuesday 2026-06-16, expiry 2026-06-24, dte=8 -> bucket "6-8"):
  DECISION cost_gate (reject, candidate)
  DECISION halted (pre-signal gate -- NOT a candidate); agg_structural/agg_remaining_stop/
           agg_gap_stress_1_5 = 999 each, to prove risk-telemetry maxima include gate rows
  DECISION filled, credit_quality_mult=1.0 (full), expected_executable_credit=2.00
  OPEN     575/565 qty=3 credit=2.05  (entry #1 of the day; tier=full; slippage=+0.05)
  CLOSE    540/530 (opened day 1) closes day 2: gross_pnl=-60, net_pnl=-65, pnl=-65
  CLOSE    575/565 (opened day 2) closes day 2: gross_pnl=20, net_pnl=15, pnl=15
  SHADOW   shadow_score=0.75, shadow_action=add_hedge

Derived expectations (weekly window covers both days):
  gross_pnl total = 40 + (-60) + 20 = 0
  net_pnl total   = 40 + (-65) + 15 = -10
  fill rate = 3 filled OPENs / 5 candidate DECISIONs (excludes the 1 "halted" gate) = 0.6
  reject_counts = {credit_too_low: 1, cost_gate: 1}; pre_signal_gate_counts = {halted: 1}
  slippage: 3 matched pairs, diff = +0.05 each -> avg/min/max = 0.05
  risk maxima (agg_* incl. the halted row) = 999 each
  same-day structural: day1 (568/558 + 540/530) = (10-1.70)*100*2 + (10-1.15)*100*1
                        = 1660.0 + 885.0 = 2545.0 (max across days)
                        day2 (575/565) = (10-2.05)*100*3 = 2385.0
  expiration structural: 2026-06-19 (day1's two opens) = 2545.0; 2026-06-24 (day2) = 2385.0
  drawdown: curve 40 -> -25 (dd 65) -> -10 (peak stays 40) => max_drawdown = 65.0
  worst session: day1 sum=+40, day2 sum=-65+15=-50 -> worst = 2026-06-16, -50.0
  by tier: full={568/558 pnl 40, 575/565 pnl 15} -> count2 total55 win_rate1.0
           probe={540/530 pnl -65} -> count1 total-65 win_rate0.0
  by dte bucket: "4-5"={40,-65} total-25 win_rate0.5; "6-8"={15} total15 win_rate1.0
  by weekday: Monday={40,-65} total-25 win_rate0.5; Tuesday={15} total15 win_rate1.0
  by entry#: 1={40 (day1#1), 15 (day2#1)} total55 win_rate1.0; 2={-65 (day1#2)} total-65 win_rate0.0
  shadow: action_counts={half_size:1, add_hedge:1}; score min0.42 max0.75 mean0.585 median0.585
"""
import csv

from bot.ops.report import build_report, render_markdown, DEFAULT_ALLOCATED_EQUITY

FIELDS = ["event", "date", "ticker", "short", "long", "expiry", "qty", "credit", "action",
         "exit_value", "pnl", "status", "gross_pnl", "net_pnl",
         "decision", "flags", "positions_today", "positions_in_expiry",
         "adjacent_strike_distance", "agg_remaining_stop", "agg_structural", "agg_gap_stress_1_5",
         "credit_ratio", "credit_pctl40", "credit_sample_count", "credit_threshold",
         "credit_quality_mult", "cost_gross_target", "cost_round_trip", "cost_target_ratio",
         "expected_executable_credit",
         "shadow_score", "shadow_action"]

ROWS = [
    # ── Day 1: Monday 2026-06-15 ──
    {"event": "DECISION", "date": "2026-06-15", "decision": "credit_too_low",
     "agg_structural": 100, "agg_remaining_stop": 50, "agg_gap_stress_1_5": 80},
    {"event": "DECISION", "date": "2026-06-15", "decision": "filled",
     "credit_quality_mult": 1.0, "expected_executable_credit": 1.65,
     "agg_structural": 150, "agg_remaining_stop": 60, "agg_gap_stress_1_5": 100},
    {"event": "OPEN", "date": "2026-06-15", "ticker": "SPY", "short": 568.0, "long": 558.0,
     "expiry": "2026-06-19", "qty": 2, "credit": 1.70, "status": "filled"},
    {"event": "DECISION", "date": "2026-06-15", "decision": "filled",
     "credit_quality_mult": 0.40, "expected_executable_credit": 1.10,
     "agg_structural": 250, "agg_remaining_stop": 90, "agg_gap_stress_1_5": 150},
    {"event": "OPEN", "date": "2026-06-15", "ticker": "SPY", "short": 540.0, "long": 530.0,
     "expiry": "2026-06-19", "qty": 1, "credit": 1.15, "status": "filled"},
    {"event": "CLOSE", "date": "2026-06-15", "ticker": "SPY", "short": 568.0, "long": 558.0,
     "expiry": "2026-06-19", "qty": 2, "credit": 1.70, "action": "take_profit",
     "exit_value": 1.30, "pnl": 40.0, "status": "filled"},
    {"event": "SHADOW", "date": "2026-06-15", "shadow_score": 0.42, "shadow_action": "half_size"},

    # ── Day 2: Tuesday 2026-06-16 ──
    {"event": "DECISION", "date": "2026-06-16", "decision": "cost_gate",
     "agg_structural": 200, "agg_remaining_stop": 70, "agg_gap_stress_1_5": 120},
    {"event": "DECISION", "date": "2026-06-16", "decision": "halted",
     "agg_structural": 999, "agg_remaining_stop": 999, "agg_gap_stress_1_5": 999},
    {"event": "DECISION", "date": "2026-06-16", "decision": "filled",
     "credit_quality_mult": 1.0, "expected_executable_credit": 2.00,
     "agg_structural": 300, "agg_remaining_stop": 110, "agg_gap_stress_1_5": 200},
    {"event": "OPEN", "date": "2026-06-16", "ticker": "SPY", "short": 575.0, "long": 565.0,
     "expiry": "2026-06-24", "qty": 3, "credit": 2.05, "status": "filled"},
    {"event": "CLOSE", "date": "2026-06-16", "ticker": "SPY", "short": 540.0, "long": 530.0,
     "expiry": "2026-06-19", "qty": 1, "credit": 1.15, "action": "stop",
     "exit_value": 1.75, "gross_pnl": -60.0, "net_pnl": -65.0, "pnl": -65.0, "status": "filled"},
    {"event": "CLOSE", "date": "2026-06-16", "ticker": "SPY", "short": 575.0, "long": 565.0,
     "expiry": "2026-06-24", "qty": 3, "credit": 2.05, "action": "take_profit",
     "exit_value": 1.85, "gross_pnl": 20.0, "net_pnl": 15.0, "pnl": 15.0, "status": "filled"},
    {"event": "SHADOW", "date": "2026-06-16", "shadow_score": 0.75, "shadow_action": "add_hedge"},
]


def _write_log(tmp_path, rows=ROWS, name="trades_alldays.csv"):
    p = tmp_path / name
    with open(str(p), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return str(p)


def _weekly_report(tmp_path, **over):
    path = _write_log(tmp_path)
    kwargs = dict(period="weekly", asof="2026-06-16", allocated_equity=DEFAULT_ALLOCATED_EQUITY)
    kwargs.update(over)
    return build_report(path, **kwargs)


# ── gross/net P&L, fallback to pnl ────────────────────────────────────────────────────────────

def test_gross_and_net_pnl_with_fallback_to_pnl(tmp_path):
    r = _weekly_report(tmp_path)
    assert r["gross_pnl"] == 0.0      # 40 + (-60) + 20
    assert r["net_pnl"] == -10.0      # 40 + (-65) + 15


def test_daily_window_scopes_to_a_single_date(tmp_path):
    path = _write_log(tmp_path)
    r = build_report(path, period="daily", asof="2026-06-15", allocated_equity=DEFAULT_ALLOCATED_EQUITY)
    assert r["window_start"] == "2026-06-15" and r["window_end"] == "2026-06-15"
    assert r["gross_pnl"] == 40.0 and r["net_pnl"] == 40.0   # only day-1's CLOSE


# ── fill rate + reject/gate counts ────────────────────────────────────────────────────────────

def test_fill_rate_and_reject_counts(tmp_path):
    r = _weekly_report(tmp_path)
    fill = r["fill"]
    assert fill["candidate_decisions"] == 5     # excludes the 1 "halted" gate
    assert fill["filled"] == 3
    assert fill["fill_rate"] == 0.6
    assert fill["reject_counts"] == {"credit_too_low": 1, "cost_gate": 1}
    assert fill["pre_signal_gate_counts"] == {"halted": 1}


# ── slippage (best-effort proxy) ──────────────────────────────────────────────────────────────

def test_slippage_matches_expected_executable_credit_to_actual_open_credit(tmp_path):
    r = _weekly_report(tmp_path)
    slip = r["slippage"]
    assert slip["available"] is True
    assert slip["count"] == 3
    assert slip["avg"] == 0.05 and slip["min"] == 0.05 and slip["max"] == 0.05


def test_slippage_unavailable_note_when_no_expected_executable_credit(tmp_path):
    rows = [r for r in ROWS if r.get("event") != "DECISION"]
    path = _write_log(tmp_path, rows=rows, name="trades_noslip.csv")
    r = build_report(path, period="weekly", asof="2026-06-16")
    assert r["slippage"]["available"] is False
    assert "not present" in r["slippage"]["note"]


# ── by credit-quality tier / DTE bucket / weekday / entry-number ─────────────────────────────

def test_performance_by_credit_tier(tmp_path):
    r = _weekly_report(tmp_path)
    tiers = r["by_credit_tier"]
    assert tiers["full"] == {"count": 2, "total_pnl": 55.0, "avg_pnl": 27.5, "win_rate": 1.0}
    assert tiers["probe"] == {"count": 1, "total_pnl": -65.0, "avg_pnl": -65.0, "win_rate": 0.0}


def test_performance_by_dte_bucket(tmp_path):
    r = _weekly_report(tmp_path)
    b = r["by_dte_bucket"]
    assert b["4-5"]["count"] == 2 and b["4-5"]["total_pnl"] == -25.0 and b["4-5"]["win_rate"] == 0.5
    assert b["6-8"] == {"count": 1, "total_pnl": 15.0, "avg_pnl": 15.0, "win_rate": 1.0}


def test_performance_by_weekday(tmp_path):
    r = _weekly_report(tmp_path)
    w = r["by_weekday"]
    assert w["Monday"]["count"] == 2 and w["Monday"]["total_pnl"] == -25.0
    assert w["Tuesday"] == {"count": 1, "total_pnl": 15.0, "avg_pnl": 15.0, "win_rate": 1.0}


def test_performance_by_entry_number_within_day(tmp_path):
    r = _weekly_report(tmp_path)
    e = r["by_entry_number"]
    assert e[1] == {"count": 2, "total_pnl": 55.0, "avg_pnl": 27.5, "win_rate": 1.0}
    assert e[2] == {"count": 1, "total_pnl": -65.0, "avg_pnl": -65.0, "win_rate": 0.0}


# ── risk exposure (same-day / expiry / total-structural / gap-stress) ───────────────────────

def test_risk_telemetry_maxima_include_pre_signal_gate_rows(tmp_path):
    r = _weekly_report(tmp_path)
    risk = r["risk"]
    assert risk["max_total_structural_risk"] == 999
    assert risk["max_total_remaining_stop_risk"] == 999
    assert risk["max_gap_stress_1_5"] == 999


def test_same_day_and_expiration_structural_risk(tmp_path):
    r = _weekly_report(tmp_path)
    risk = r["risk"]
    assert risk["max_same_day_structural_risk"] == 2545.0
    assert risk["max_expiration_structural_risk"] == 2545.0


# ── max drawdown + worst session ─────────────────────────────────────────────────────────────

def test_max_drawdown_and_worst_session(tmp_path):
    r = _weekly_report(tmp_path)
    dd = r["drawdown"]
    assert dd["max_drawdown"] == 65.0
    assert dd["worst_session_date"] == "2026-06-16"
    assert dd["worst_session_pnl"] == -50.0


# ── shadow-regime summary ────────────────────────────────────────────────────────────────────

def test_shadow_action_counts_and_score_distribution(tmp_path):
    r = _weekly_report(tmp_path)
    shadow = r["shadow"]
    assert shadow["count"] == 2
    assert shadow["action_counts"] == {"half_size": 1, "add_hedge": 1}
    dist = shadow["score_distribution"]
    assert dist["min"] == 0.42 and dist["max"] == 0.75
    assert dist["mean"] == 0.585 and dist["median"] == 0.585


# ── returns use ALLOCATED equity, never an account balance ──────────────────────────────────

def test_returns_use_allocated_equity_not_account_balance(tmp_path):
    r72k = _weekly_report(tmp_path, allocated_equity=72000.0)
    r50k = _weekly_report(tmp_path, allocated_equity=50000.0)
    assert r72k["allocated_equity"] == 72000.0
    assert r72k["return_pct_of_allocated_equity"] == round(-10.0 / 72000.0, 4)
    # same net P&L, DIFFERENT allocated_equity -> DIFFERENT return_pct: proves the denominator is
    # the injected allocated_equity, not some fixed/account-balance number baked into the module.
    assert r50k["return_pct_of_allocated_equity"] == round(-10.0 / 50000.0, 4)
    assert r72k["return_pct_of_allocated_equity"] != r50k["return_pct_of_allocated_equity"]
    assert r72k["drawdown"]["max_drawdown_pct_of_allocated_equity"] == round(65.0 / 72000.0, 4)


def test_default_allocated_equity_is_72000(tmp_path):
    path = _write_log(tmp_path)
    r = build_report(path, period="weekly", asof="2026-06-16")
    assert r["allocated_equity"] == 72000.0
    assert DEFAULT_ALLOCATED_EQUITY == 72000.0


# ── markdown rendering ────────────────────────────────────────────────────────────────────────

def test_render_markdown_produces_every_section(tmp_path):
    r = _weekly_report(tmp_path)
    md = render_markdown(r)
    for heading in ("# S2b All-Days Report", "## P&L", "## Fill Rate & Order Outcomes",
                   "## Actual Slippage", "## Performance by Credit-Quality Tier",
                   "## Performance by DTE Bucket", "## Performance by Weekday",
                   "## Performance by Entry Number", "## Risk Exposure",
                   "## Drawdown & Worst Session", "## Shadow-Regime Summary"):
        assert heading in md
    assert "72,000.00" in md
    assert "credit_too_low" in md
    assert "half_size" in md


def test_render_markdown_handles_empty_report_gracefully(tmp_path):
    path = _write_log(tmp_path, rows=[], name="empty.csv")
    r = build_report(path, period="daily")
    md = render_markdown(r)
    assert "# S2b All-Days Report" in md   # never raises on an empty log


# ── malformed / missing fields never raise (best-effort, spec-wide convention) ─────────────────

def test_missing_columns_degrade_to_none_not_crash(tmp_path):
    minimal_fields = ["event", "date", "ticker", "short", "long", "expiry", "qty", "credit",
                      "action", "exit_value", "pnl", "status"]   # the original 12-column shape
    p = tmp_path / "trades_live.csv"
    with open(str(p), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=minimal_fields)
        w.writeheader()
        w.writerow({"event": "OPEN", "date": "2026-06-15", "ticker": "SPY", "short": 568.0,
                   "long": 558.0, "expiry": "2026-06-19", "qty": 1, "credit": 1.7,
                   "action": "", "exit_value": "", "pnl": "", "status": "filled"})
        w.writerow({"event": "CLOSE", "date": "2026-06-15", "ticker": "SPY", "short": 568.0,
                   "long": 558.0, "expiry": "2026-06-19", "qty": 1, "credit": 1.7,
                   "action": "take_profit", "exit_value": 1.3, "pnl": 40.0, "status": "filled"})
        w.writerow({"event": "SHADOW", "date": "2026-06-15"})   # no shadow columns at all
    r = build_report(str(p), period="daily")
    assert r["net_pnl"] == 40.0
    assert r["slippage"]["available"] is False
    assert r["risk"]["max_total_structural_risk"] is None
    assert r["shadow"]["score_distribution"] is None


def test_nonexistent_trade_log_returns_empty_report_not_crash(tmp_path):
    r = build_report(str(tmp_path / "does_not_exist.csv"), period="daily")
    assert r["gross_pnl"] == 0.0 and r["net_pnl"] == 0.0
    render_markdown(r)   # must not raise


def test_invalid_period_raises_value_error(tmp_path):
    path = _write_log(tmp_path)
    import pytest
    with pytest.raises(ValueError):
        build_report(path, period="monthly")
