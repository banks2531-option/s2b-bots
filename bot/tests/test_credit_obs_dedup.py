"""Task 7 (post-v2 refinement §13): the adaptive credit-history dedup must use the SAME notion of a
"new candidate" as §12's candidate_key.

    should_record_credit_observation = new_candidate_key or abs(ratio - last_ratio) >= 0.005

Two things were out of line before this task:

1. The 15-minute window was computed as `minutes_since_open // 15` here but as `(hour, minute // 15)`
   in candidate_key. Both bucket by 15 minutes, but on DIFFERENT boundaries (the session opens at
   9:30, so minutes-since-open buckets straddle the clock quarter-hours). Two "identical" dedup
   rules that disagree about when a window rolls is exactly the kind of drift that makes the
   adaptive threshold hard to reason about.

2. The >= 0.005 comparison was done on raw binary floats, so it was ASYMMETRIC: a +0.005 ratio move
   recorded an observation but an identical -0.005 move did not. That biased the adaptive credit
   history toward upward moves. Live on both bots before this fix.
"""
from datetime import datetime

from bot.strategy.credit_quality import should_record_observation, bucket15_of


def _last(**over):
    base = dict(date="2026-07-20", expiry="2026-07-31", short=745.0, long=735.0,
                ratio=0.1200, bucket15=bucket15_of(datetime(2026, 7, 20, 10, 1)))
    base.update(over)
    return base


def _call(last, **over):
    base = dict(date="2026-07-20", expiry="2026-07-31", short=745.0, long=735.0,
                ratio=0.1200, bucket15=bucket15_of(datetime(2026, 7, 20, 10, 1)))
    base.update(over)
    return should_record_observation(last, **base)


# ── the bucket now matches candidate_key's (hour, minute // 15) ──────────────────────────────────

def test_bucket15_is_hour_qualified_and_json_safe():
    """A plain int (hour*4 + minute//15) rather than candidate_key's tuple, so it round-trips through
    the JSON-persisted credit_obs_last without the tuple-becomes-list problem -- while still
    identifying exactly the same window."""
    b = bucket15_of(datetime(2026, 7, 20, 10, 1))
    assert isinstance(b, int)
    assert b == 10 * 4 + 0
    assert bucket15_of(datetime(2026, 7, 20, 10, 8)) == b       # same window
    assert bucket15_of(datetime(2026, 7, 20, 10, 16)) != b      # next window
    assert bucket15_of(datetime(2026, 7, 20, 11, 1)) != b       # same sub-bucket, later hour


def test_window_boundaries_sit_on_the_clock_quarter_hour_not_the_session_open():
    """9:44 -> 9:45 must roll the window (a clock quarter-hour). Under the old minutes-since-open
    scheme the boundaries fell at 9:45/10:00 only by coincidence of the 9:30 open, and any other
    open time would have desynced them from candidate_key entirely."""
    assert bucket15_of(datetime(2026, 7, 20, 9, 44)) != bucket15_of(datetime(2026, 7, 20, 9, 45))
    assert bucket15_of(datetime(2026, 7, 20, 9, 45)) == bucket15_of(datetime(2026, 7, 20, 9, 59))


# ── §13: new key OR a material ratio move ────────────────────────────────────────────────────────

def test_first_observation_is_always_recorded():
    assert should_record_observation(None, date="d", expiry="e", short=1.0, long=2.0,
                                     ratio=0.12, bucket15=40) is True


def test_identical_candidate_in_same_window_is_not_recorded():
    assert _call(_last()) is False


def test_new_strike_expiry_or_day_is_recorded():
    assert _call(_last(), short=746.0) is True
    assert _call(_last(), long=736.0) is True
    assert _call(_last(), expiry="2026-08-07") is True
    assert _call(_last(), date="2026-07-21") is True


def test_new_15_minute_window_is_recorded():
    assert _call(_last(), bucket15=bucket15_of(datetime(2026, 7, 20, 10, 16))) is True


def test_material_ratio_move_is_recorded_symmetrically():
    """The regression this task fixes: BOTH directions of an exact 0.005 move must record."""
    assert _call(_last(), ratio=0.1250) is True     # +0.005
    assert _call(_last(), ratio=0.1150) is True     # -0.005  (was False: 0.004999999999999991)


def test_immaterial_ratio_move_is_not_recorded():
    assert _call(_last(), ratio=0.1240) is False
    assert _call(_last(), ratio=0.1160) is False


def test_dedup_rule_agrees_with_is_new_candidate():
    """§13 is defined as "new_candidate_key or ratio move" -- so for a fixed key the two helpers must
    never disagree about a given ratio delta."""
    from bot.strategy.credit_quality import is_new_candidate
    for delta in (0.0, 0.001, 0.0049, 0.005, 0.0051, 0.01, -0.001, -0.005, -0.0051, -0.01):
        ratio = round(0.12 + delta, 6)
        assert _call(_last(), ratio=ratio) is is_new_candidate(
            key=("k",), ratio=ratio, seen={("k",): 0.12})
