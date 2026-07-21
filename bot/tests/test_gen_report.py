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

_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "reports", "gen_report.py"))
_spec = importlib.util.spec_from_file_location("gen_report", _PATH)
gen_report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen_report)


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
