"""Tests for dashboard/build_dashboard.py (the S2b two-bot dashboard builder).

Imported by file path because dashboard/ is not a package (mirrors test_gen_report.py).
Covers: load_reports, daily_pnl_series (excludes corrupt Bot B pre-07-20),
health_verdict (each branch), parse_recommendations, why_line, render_html smoke,
and the build() entrypoint.
"""
import importlib.util
import json
import os

_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "dashboard", "build_dashboard.py"))
_spec = importlib.util.spec_from_file_location("build_dashboard", _PATH)
bd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bd)


def _write_fixture(tmp_path):
    latest = tmp_path / "latest"
    latest.mkdir()
    (latest / "bot_c_performance.json").write_text(json.dumps({
        "bot": "Bot C", "equity": 900.0, "realized_report_date": 0, "realized_is_stale": True,
        "risk_day": "2026-07-17", "report_date": "2026-07-22", "lifetime_realized": -17.0,
        "pnl_source": "synthetic_mark", "pnl_validation_status": "live_fill_accounting_disabled",
        "entries_today": 0, "opens_today": [], "closes_today": [{"pnl": "26"}],
        "funnel": {"entry_cycles_started": 1091, "unique_candidate_opportunities": 219},
        "gate_breakdown": {"risk_budget:gap_1atr": 234, "off_hours": 29},
        "positions": [], "unrealized": 0,
        "history": {"total_realized": -17.0, "closed_trades": 15, "wins": 10, "losses": 5,
                    "win_rate_pct": 66.7, "profit_factor": 0.86,
                    "by_day": {"2026-07-15": 11.0, "2026-07-17": -14.6}}}))
    (latest / "broker_snapshot_c.json").write_text(json.dumps({"account": "6YB7****", "legs": [],
        "pending_orders": [], "reconciliation": {"owned_legs": 0, "foreign_legs": 0}}))
    # minimal bot_b twins
    (latest / "bot_b_performance.json").write_text(json.dumps({"bot": "Bot B", "equity": 74000.0,
        "realized_report_date": 9.0, "realized_is_stale": False, "risk_day": "2026-07-22",
        "report_date": "2026-07-22", "lifetime_realized": 4077.0, "pnl_source": "actual_fill",
        "pnl_validation_status": "sandbox_unaudited", "entries_today": 1,
        "opens_today": [{}], "closes_today": [{"pnl": "138.8"}],
        "funnel": {"entry_cycles_started": 1101, "unique_candidate_opportunities": 228},
        "gate_breakdown": {"duplicate_strikes": 295, "risk_budget:gap_2atr": 33, "filled": 1},
        "positions": [{}, {}, {}], "unrealized": 9,
        "history": {"total_realized": 4077.0, "closed_trades": 22, "wins": 19, "losses": 3,
                    "win_rate_pct": 86.4, "profit_factor": 2.07,
                    "by_day": {"2026-07-02": -2289.0, "2026-07-21": 293.6, "2026-07-22": 138.8}}}))
    (latest / "broker_snapshot_b.json").write_text(json.dumps({"account": "VA47****",
        "legs": [], "pending_orders": [], "reconciliation": {"owned_legs": 4, "foreign_legs": 2}}))
    (latest / "market_context.json").write_text(json.dumps({"spot": 747.41,
        "daily": {"open": 746.62, "high": 750.02, "low": 746.37, "close": 747.41},
        "vix": 16.6, "session_vwap": 748.30, "price_vs_vwap_pts": -0.94}))
    am = tmp_path / "advisor_memory.md"
    am.write_text("| Date | Recommendation | Reason (evidence) | Confidence | Status | Implemented | Outcome |\n"
                  "|---|---|---|---|---|---|---|\n"
                  "| 2026-07-22 | Do X | because Y | Very High | Pending | - | - |\n"
                  "| _example_ | ignore | - | Moderate | Pending | - | - |\n")
    return str(latest), str(am)


# ---------------- Task 1: load_reports ----------------
def test_load_reports_pulls_both_bots_and_market(tmp_path):
    latest, am = _write_fixture(tmp_path)
    d = bd.load_reports(latest, am)
    assert d["b"]["perf"]["bot"] == "Bot B"
    assert d["c"]["perf"]["bot"] == "Bot C"
    assert d["market"]["spot"] == 747.41
    assert d["b"]["broker"]["reconciliation"]["foreign_legs"] == 2


# ---------------- Task 2: daily_pnl_series ----------------
def test_daily_pnl_series_cumulative_and_sorted():
    perf = {"history": {"by_day": {"2026-07-22": 138.8, "2026-07-21": 293.6, "2026-07-02": -2289.0}}}
    s = bd.daily_pnl_series(perf, "c")   # non-B: no exclusion
    assert [r["date"] for r in s] == ["2026-07-02", "2026-07-21", "2026-07-22"]
    assert s[-1]["cumulative"] == round(-2289.0 + 293.6 + 138.8, 2)


def test_daily_pnl_series_excludes_corrupt_bot_b_pre_0720():
    perf = {"history": {"by_day": {"2026-07-15": 1742.4, "2026-07-20": -1104.0, "2026-07-22": 138.8}}}
    s = bd.daily_pnl_series(perf, "b")   # Bot B: drop dates < 2026-07-20
    assert [r["date"] for r in s] == ["2026-07-20", "2026-07-22"]


# ---------------- Task 3: health_verdict ----------------
def test_health_stalled_when_not_trading_and_risk_capped():
    perf = {"entries_today": 0, "positions": [], "pnl_source": "synthetic_mark",
            "gate_breakdown": {"risk_budget:gap_1atr": 234, "off_hours": 29},
            "history": {"by_day": {"2026-07-17": -14.6}, "win_rate_pct": 66.7, "profit_factor": 0.86}}
    v = bd.health_verdict(perf, "c")
    assert v["status"] == "Stalled"
    assert "risk budget" in v["narrative"].lower()
    assert "synthetic" in v["narrative"].lower()


def test_health_improving_when_recent_green_and_high_winrate():
    perf = {"entries_today": 1, "positions": [{}], "pnl_source": "actual_fill",
            "gate_breakdown": {"filled": 1, "duplicate_strikes": 295},
            "history": {"by_day": {"2026-07-20": 40.0, "2026-07-21": 293.6, "2026-07-22": 138.8},
                        "win_rate_pct": 86.4, "profit_factor": 2.07}}
    v = bd.health_verdict(perf, "b")
    assert v["status"] == "Improving"


def test_health_insufficient_when_no_clean_days():
    v = bd.health_verdict({"history": {"by_day": {}}}, "c")
    assert v["status"] == "Insufficient data"


# ---------------- Task 4: parse_recommendations ----------------
def test_parse_recommendations_splits_and_skips_examples():
    md = ("| Date | Recommendation | Reason (evidence) | Confidence | Status | Implemented | Outcome |\n"
          "|---|---|---|---|---|---|---|\n"
          "| 2026-07-22 | Export cap fields | blank telemetry | Very High | Pending | - | - |\n"
          "| 2026-07-19 | Book reconcile P&L | drift | High | Implemented | 2026-07-20 | fixed |\n"
          "| _example_ | ignore me | - | Moderate | Pending | - | - |\n")
    rows = bd.parse_recommendations(md)
    assert len(rows) == 2
    assert rows[0]["date"] == "2026-07-22" and rows[0]["status"] == "Pending"
    assert rows[1]["implemented"] == "2026-07-20"
    assert all("example" not in r["date"].lower() for r in rows)


# ---------------- Task 5: why_line ----------------
def test_why_line_traded():
    perf = {"opens_today": [{}], "closes_today": [{}, {}], "entries_today": 1,
            "gate_breakdown": {"filled": 1, "risk_budget:gap_2atr": 33}}
    assert "opened 1" in bd.why_line(perf).lower()


def test_why_line_stalled_names_the_gate():
    perf = {"opens_today": [], "closes_today": [], "entries_today": 0, "positions": [],
            "gate_breakdown": {"risk_budget:gap_1atr": 234, "off_hours": 29}}
    line = bd.why_line(perf).lower()
    assert "no new" in line and "gap_1atr" in line


# ---------------- Task 6: render_html ----------------
def test_render_html_two_bots_no_bot_a_and_has_tabs(tmp_path):
    latest, am = _write_fixture(tmp_path)
    data = bd.load_reports(latest, am)
    doc = bd.render_html(data, generated_at="2026-07-22T17:00")
    assert doc.lstrip().lower().startswith("<!doctype html")
    assert "Bot B" in doc and "Bot C" in doc
    assert "Bot A" not in doc
    for tab in ("Overview", "Performance", "Recommendations"):
        assert tab in doc
    assert "synthetic" in doc.lower()          # Bot C provenance surfaced
    assert "Do X" in doc                        # recommendation row rendered
    assert "http://" not in doc and "https://" not in doc   # self-contained, no external refs


# ---------------- Task 7: build() ----------------
def test_build_writes_html_file(tmp_path):
    latest, am = _write_fixture(tmp_path)
    out = tmp_path / "dashboard.html"
    bd.build(latest, am, str(out))
    doc = out.read_text()
    assert "Bot C" in doc and "Bot A" not in doc
