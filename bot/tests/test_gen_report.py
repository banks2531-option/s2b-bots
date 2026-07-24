"""Regression tests for reports/gen_report.py (the on-droplet report generator).

Imported by file path because reports/ is not a package. These lock in the Codex-readiness fixes:
  item 4 -- manifest hashes every file's bytes uniformly, incl. deployed_commit.txt
  item 5 -- realized-P&L fields never present a prior risk-day's P&L as today's
  item 6 -- explicit P&L provenance instead of the ambiguous actual_fill_accounting boolean
  item 1 -- shared-account broker legs tagged owned vs foreign
"""
import hashlib
import importlib.util
import os
from datetime import datetime

_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "reports", "gen_report.py"))
_spec = importlib.util.spec_from_file_location("gen_report", _PATH)
gen_report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen_report)


# ---------------- broker-real unrealized (matches the broker app, not synthetic) ----------------
def test_spread_broker_pnl_matches_the_broker():
    # Bot C 729/724 on 2026-07-23: short 729 mark 3.76 / cost_basis -517; long 724 mark 2.77 / cb 396
    # broker shows short +$141, long -$119 -> net +$22 (vs the synthetic -$26 the old calc produced)
    assert gen_report.spread_broker_pnl(3.76, 2.77, 1, -517, 396) == 22.0
    # any missing input -> None so the caller falls back to the synthetic estimate
    assert gen_report.spread_broker_pnl(None, 2.77, 1, -517, 396) is None
    assert gen_report.spread_broker_pnl(3.76, 2.77, 1, None, 396) is None


# ---------------- item 4: uniform manifest byte-SHA ----------------
def test_sha16_is_first16_of_sha256():
    assert gen_report.sha16(b"abc") == hashlib.sha256(b"abc").hexdigest()[:16]


def test_manifest_hashes_deployed_commit_as_file_bytes_like_every_other(tmp_path):
    (tmp_path / "market_context.json").write_text('{"x": 1}\n')
    commit = "f306ae17c3f513054890b47396717ec1c7c2da6a"
    (tmp_path / "deployed_commit.txt").write_text(commit + "\n")
    names = ["market_context.json", "deployed_commit.txt"]

    m = gen_report.manifest_of_dir(str(tmp_path), names)

    for n in names:
        expect = hashlib.sha256((tmp_path / n).read_bytes()).hexdigest()[:16]
        assert m[n] == expect, n
    # the historic bug: deployed_commit.txt recorded the commit-string prefix, not a byte hash
    assert m["deployed_commit.txt"] != commit[:16]


# ---------------- item 5: honest realized-P&L fields ----------------
def test_realized_stale_counter_is_not_reported_as_today():
    # Bot C shape on 2026-07-21: the -14.6 counter belongs to risk_day 2026-07-17, no closes today.
    state = {"risk_day": "2026-07-17", "realized_today": -14.6}
    rf = gen_report.realized_fields(state, "2026-07-21", closes_today=[], lifetime=-16.6)
    assert rf["report_date"] == "2026-07-21"
    assert rf["risk_day"] == "2026-07-17"
    assert rf["realized_is_stale"] is True
    assert rf["realized_risk_day"] == -14.6      # counter preserved, honestly labeled
    assert rf["realized_report_date"] == 0.0     # nothing was actually booked today
    assert rf["lifetime_realized"] == -16.6


def test_realized_current_day_matches_todays_closes():
    # Bot B shape: risk_day == report_date, and today's closes sum to the counter.
    state = {"risk_day": "2026-07-21", "realized_today": 293.6}
    closes = [{"pnl": "138.8"}, {"pnl": "75.4"}, {"pnl": "79.4"}]
    rf = gen_report.realized_fields(state, "2026-07-21", closes_today=closes, lifetime=4077.0)
    assert rf["realized_is_stale"] is False
    assert rf["realized_report_date"] == 293.6


# ---------------- item 6: explicit P&L provenance ----------------
def test_pnl_provenance_actual_vs_synthetic():
    on = gen_report.pnl_accounting_fields({"actual_fill_accounting": True})
    off = gen_report.pnl_accounting_fields({"actual_fill_accounting": False})
    assert on["pnl_source"] == "actual_fill"
    assert off["pnl_source"] == "synthetic_mark"
    assert on["pnl_validation_status"] != off["pnl_validation_status"]


# ---------------- item 3: DST-safe slot gating (cron does not honor CRON_TZ) ----------------
def _et(h, m, day=21):
    # 2026-07-21 is a Tuesday; 2026-07-25 is a Saturday.
    return datetime(2026, 7, day, h, m, tzinfo=gen_report.ET)


def test_due_now_fires_exactly_at_each_slot_and_skips_between():
    for h, m in [(9, 40), (10, 0), (12, 0), (14, 0), (15, 45), (16, 15)]:
        assert gen_report.due_now(_et(h, m)) is True, (h, m)
    assert gen_report.due_now(_et(10, 1)) is True      # within tol of 10:00
    assert gen_report.due_now(_et(10, 5)) is False     # the next */5 tick, 5 min off -> excluded
    assert gen_report.due_now(_et(11, 0)) is False     # not a slot
    assert gen_report.due_now(_et(13, 0)) is False     # after hours (no slot)


def test_due_now_skips_weekends():
    assert gen_report.due_now(_et(10, 0, day=25)) is False   # Saturday


# ---------------- item 1: shared-account leg labeling ----------------
def test_shared_account_foreign_spread_is_labeled_foreign():
    # Bot B book: 740/730 and 741/731 (July 29). Broker also shows a co-occupant's 736/726.
    own = [{"short_strike": 740.0, "long_strike": 730.0, "expiry": "2026-07-29"},
           {"short_strike": 741.0, "long_strike": 731.0, "expiry": "2026-07-29"}]
    legs = [
        {"symbol": "SPY260729P00726000", "quantity": 1.0},
        {"symbol": "SPY260729P00730000", "quantity": 1.0},
        {"symbol": "SPY260729P00731000", "quantity": 1.0},
        {"symbol": "SPY260729P00736000", "quantity": -1.0},
        {"symbol": "SPY260729P00740000", "quantity": -1.0},
        {"symbol": "SPY260729P00741000", "quantity": -1.0},
    ]

    labeled, summary = gen_report.label_broker_legs(legs, own)

    by_sym = {L["symbol"]: L["ownership"] for L in labeled}
    assert by_sym["SPY260729P00736000"] == "foreign"
    assert by_sym["SPY260729P00726000"] == "foreign"
    assert by_sym["SPY260729P00740000"] == "owned"
    assert by_sym["SPY260729P00730000"] == "owned"
    assert summary["owned_legs"] == 4
    assert summary["foreign_legs"] == 2


def test_a_short_leg_matched_only_when_side_agrees():
    # Same strike/expiry as the book's SHORT 740, but held LONG -> a co-occupant's leg, not ours.
    own = [{"short_strike": 740.0, "long_strike": 730.0, "expiry": "2026-07-29"}]
    legs = [{"symbol": "SPY260729P00740000", "quantity": 1.0}]   # +1 (long), book has it short
    labeled, summary = gen_report.label_broker_legs(legs, own)
    assert labeled[0]["ownership"] == "foreign"
    assert summary["foreign_legs"] == 1


# ---------------- blocker 2: VWAP integrity (Tradier per-bar vwap is unreliable) ----------------
def test_vwap_uses_self_computed_session_vwap_not_bad_feed_values():
    # bar 1's Tradier vwap (90) is impossible -- outside its own [99,100]. The derived vwap_now must
    # come from the self-computed session VWAP (typical*vol), which stays inside the day's range.
    bars = [
        {"high": 100.0, "low": 99.0, "close": 99.5, "volume": 100, "vwap": 90.0},
        {"high": 101.0, "low": 100.0, "close": 100.5, "volume": 300, "vwap": 100.4},
    ]
    r = gen_report.vwap_fields(bars)
    assert r["vwap_bars_out_of_range"] == 1
    assert r["session_vwap"] == 100.25          # ((99.5*100)+(100.5*300))/400
    assert r["vwap_now"] == r["session_vwap"]    # no longer bars[-1]["vwap"]
    assert r["price_vs_vwap_pts"] == 0.25        # 100.5 - 100.25


def test_vwap_fields_without_volume_reports_only_the_quality_count():
    r = gen_report.vwap_fields([{"high": 100.0, "low": 99.0, "close": 99.5, "volume": 0, "vwap": 99.5}])
    assert r["vwap_bars_out_of_range"] == 0
    assert "session_vwap" not in r and "vwap_now" not in r


# ---------------- decision telemetry: export active quantity-cap fields, not blank legacy ----------
def test_decision_cols_use_active_telemetry_not_blank_legacy_columns():
    cols = gen_report.DECISION_COLS
    for active in ("requested_qty", "final_qty", "risk_current_exposure", "risk_limit",
                   "risk_remaining_capacity", "risk_incremental_per_contract"):
        assert active in cols, active
    # the superseded columns the orchestrator leaves blank must NOT be exported
    for legacy in ("risk_budget_proposed_qty", "risk_budget_permitted_qty", "risk_budget_headroom"):
        assert legacy not in cols, legacy


def test_decision_projection_is_nonblank_for_a_real_blocked_row():
    # a real Bot C gap_1atr block (2026-07-22): capacity exhausted, final_qty 0 -- but fully populated.
    raw = {"date": "2026-07-22", "decision": "risk_budget:gap_1atr", "limiting_gate": "gap_1atr",
           "requested_qty": "1", "quality_adjusted_qty": "1", "final_qty": "0",
           "risk_current_exposure": "0", "risk_limit": "89.97", "risk_remaining_capacity": "89.97",
           "risk_incremental_per_contract": "300.91",
           # legacy columns present-but-blank in the source, as the orchestrator leaves them:
           "risk_budget_proposed_qty": "", "risk_budget_permitted_qty": "", "risk_budget_headroom": ""}
    row = {c: raw.get(c, "") for c in gen_report.DECISION_COLS}
    for f in ("requested_qty", "final_qty", "risk_limit", "risk_remaining_capacity",
              "risk_incremental_per_contract"):
        assert row[f] != "", f
    assert row["final_qty"] == "0"
    assert row["risk_incremental_per_contract"] == "300.91"
