"""Trade-log header integrity.

FOUND IN PRODUCTION (2026-07-19, during the pre-deployment branch review). Both bots were writing
rows with 63-69 fields into CSVs whose header declared 12:

    trades_alldays.csv | header: 12 | data widths: [(63, 1020), (69, 1012), (12, 254)]
    trades_live.csv    | header: 12 | data widths: [(69, 598),  (67, 310),  (12, 33)]

Cause: make_trade_logger writes the header only when the file does not exist. Both files were
created before decision/shadow logging was enabled. When those flags were later turned on the
fieldnames grew, but the header on disk stayed at the original 12 columns -- so every subsequent row
carried unnamed trailing values.

Consequence: any header-based reader (csv.DictReader, pandas, the §17 funnel report, the advisor's
whole Bot B validation review) misparses those rows. The decision value sat at unnamed index 14. The
telemetry was being written and was unreadable, which is worse than not logging it at all -- it looks
like data.

Fix: when the on-disk header does not match the fieldnames this logger will write, ROTATE the file
aside and start a fresh one with the correct header. Rotating rather than rewriting keeps the old
data intact and never risks a partial in-place rewrite of a file the bot is actively appending to.
"""
import csv
import os

from bot.app.wiring import make_trade_logger, _LOG_FIELDS


def _read(path):
    with open(path, newline="") as fh:
        return list(csv.reader(fh))


def test_new_file_gets_a_header_matching_its_fieldnames(tmp_path):
    p = str(tmp_path / "t.csv")
    make_trade_logger(p, include_decision_columns=True)({"event": "DECISION", "decision": "filled"})
    rows = _read(p)
    assert rows[0][:12] == _LOG_FIELDS
    assert "decision" in rows[0]
    assert len(rows[1]) == len(rows[0])            # data width matches header width


def test_every_row_width_matches_the_header_width(tmp_path):
    p = str(tmp_path / "t.csv")
    log = make_trade_logger(p, include_decision_columns=True)
    log({"event": "OPEN", "qty": 2})
    log({"event": "DECISION", "decision": "risk_budget", "limiting_gate": "gap_1_5atr"})
    rows = _read(p)
    assert len({len(r) for r in rows}) == 1


def test_a_stale_narrow_header_is_rotated_and_replaced(tmp_path):
    """The exact production case: a file created with the 12-column header, then reopened by a
    logger that also writes decision columns."""
    p = str(tmp_path / "t.csv")
    make_trade_logger(p)({"event": "OPEN", "qty": 1})            # 12-column header
    assert len(_read(p)[0]) == 12

    make_trade_logger(p, include_decision_columns=True)(
        {"event": "DECISION", "decision": "filled", "limiting_gate": "expiry_stop"})
    rows = _read(p)
    assert len(rows[0]) > 12                                     # fresh, correct header
    assert "limiting_gate" in rows[0]
    assert len({len(r) for r in rows}) == 1                      # and the data matches it

    rotated = [f for f in os.listdir(str(tmp_path)) if f.startswith("t.csv.superseded")]
    assert len(rotated) == 1, "the old data must be preserved, not discarded"


def test_rotated_file_keeps_the_original_rows(tmp_path):
    p = str(tmp_path / "t.csv")
    make_trade_logger(p)({"event": "OPEN", "qty": 7})
    make_trade_logger(p, include_decision_columns=True)({"event": "DECISION", "decision": "filled"})
    rotated = [f for f in os.listdir(str(tmp_path)) if f.startswith("t.csv.superseded")][0]
    old = _read(str(tmp_path / rotated))
    assert old[0] == _LOG_FIELDS
    assert old[1][_LOG_FIELDS.index("qty")] == "7"


def test_matching_header_is_left_alone_and_appended_to(tmp_path):
    """No gratuitous rotation: a logger reopening a file it already matches must simply append."""
    p = str(tmp_path / "t.csv")
    make_trade_logger(p, include_decision_columns=True)({"event": "DECISION", "decision": "a"})
    make_trade_logger(p, include_decision_columns=True)({"event": "DECISION", "decision": "b"})
    rows = _read(p)
    assert len(rows) == 3                                        # header + two data rows
    assert not [f for f in os.listdir(str(tmp_path)) if "superseded" in f]


def test_repeated_rotation_does_not_overwrite_an_earlier_archive(tmp_path):
    p = str(tmp_path / "t.csv")
    make_trade_logger(p)({"event": "OPEN", "qty": 1})
    make_trade_logger(p, include_decision_columns=True)({"event": "DECISION", "decision": "a"})
    make_trade_logger(p)({"event": "OPEN", "qty": 2})             # back to narrow -> rotates again
    rotated = [f for f in os.listdir(str(tmp_path)) if "superseded" in f]
    assert len(rotated) == 2


def test_the_written_rows_are_readable_by_field_name(tmp_path):
    """The property that actually failed in production: DictReader must recover the values."""
    p = str(tmp_path / "t.csv")
    make_trade_logger(p, include_decision_columns=True)(
        {"event": "DECISION", "date": "2026-07-20", "decision": "risk_budget:gap_1_5atr",
         "limiting_gate": "gap_1_5atr", "final_qty": 0, "requested_qty": 3})
    row = list(csv.DictReader(open(p, newline="")))[0]
    assert row["decision"] == "risk_budget:gap_1_5atr"
    assert row["limiting_gate"] == "gap_1_5atr"
    assert row["requested_qty"] == "3"
    assert None not in row, "unnamed trailing values means the header is wrong again"
