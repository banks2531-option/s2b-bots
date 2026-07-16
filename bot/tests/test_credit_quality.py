from bot.strategy.credit_quality import credit_tier, dte_bucket, full_size_threshold


def test_reject_below_min():
    assert credit_tier(0.9, 10.0, min_ratio=0.10, full_threshold=0.115, probe_mult=0.40) == 0.0


def test_probe_band():
    assert credit_tier(1.10, 10.0, min_ratio=0.10, full_threshold=0.115, probe_mult=0.40) == 0.40


def test_full_size():
    assert credit_tier(1.20, 10.0, min_ratio=0.10, full_threshold=0.115, probe_mult=0.40) == 1.0


def test_dte_bucket():
    assert dte_bucket(4) == "4-5"
    assert dte_bucket(7) == "6-8"
    assert dte_bucket(10) == "9-11"


def test_dte_bucket_12_plus():
    assert dte_bucket(12) == "12+"
    assert dte_bucket(30) == "12+"
    assert dte_bucket(11) == "9-11"   # boundary: 11 stays in 9-11, 12 is the first 12+ day


# ── full_size_threshold (v2 §3): floor/ceiling clamp + warmup gate ──────────────────────────────

FLOOR = 0.115
CEILING = 0.14
MIN_SIGNALS = 30


def test_threshold_warmup_uses_floor_below_min_signals():
    # 29 prior signals (< 30) -- even though their p40 (0.20) is well above the ceiling, warmup
    # must return the floor untouched.
    hist = [0.20] * (MIN_SIGNALS - 1)
    assert full_size_threshold(hist, FLOOR, CEILING, MIN_SIGNALS) == FLOOR


def test_threshold_empty_history_uses_floor():
    assert full_size_threshold([], FLOOR, CEILING, MIN_SIGNALS) == FLOOR


def test_threshold_at_exactly_min_signals_adapts():
    # exactly 30 (>= min_signals) -- no longer warmup, adapts to (clamped) p40.
    hist = [0.20] * MIN_SIGNALS
    assert full_size_threshold(hist, FLOOR, CEILING, MIN_SIGNALS) == CEILING   # p40=0.20 > ceiling -> capped


def test_threshold_ceiling_caps_high_percentile():
    hist = [0.30] * MIN_SIGNALS
    assert full_size_threshold(hist, FLOOR, CEILING, MIN_SIGNALS) == CEILING


def test_threshold_floor_when_percentile_below_floor():
    hist = [0.01] * MIN_SIGNALS
    assert full_size_threshold(hist, FLOOR, CEILING, MIN_SIGNALS) == FLOOR


def test_threshold_adapts_between_floor_and_ceiling():
    # 30 ascending ratios 0.100..0.158 (step 0.002) -> p40 (index 11.6, interpolated) = 0.1232,
    # which sits strictly between floor and ceiling -> the threshold should track it (not clamp).
    hist = [round(0.100 + i * 0.002, 4) for i in range(MIN_SIGNALS)]
    thr = full_size_threshold(hist, FLOOR, CEILING, MIN_SIGNALS)
    assert FLOOR < thr < CEILING
    assert round(thr, 4) == 0.1232


# ── should_record_observation (partner review v2 item 3): dedup repeated near-identical
# observations of the SAME spread across a polling day so 60-signal history isn't flooded ──────

def test_should_record_observation_dedups():
    from bot.strategy.credit_quality import should_record_observation
    last = {"date": "2026-07-15", "expiry": "2026-07-17", "short": 743.0, "long": 733.0,
            "ratio": 0.104, "bucket15": 39}
    # same candidate, same 15-min bucket, ratio moved < 0.5pp -> do NOT record
    assert should_record_observation(last, date="2026-07-15", expiry="2026-07-17",
        short=743.0, long=733.0, ratio=0.106, bucket15=39) is False
    # ratio moved >= 0.5pp -> record
    assert should_record_observation(last, date="2026-07-15", expiry="2026-07-17",
        short=743.0, long=733.0, ratio=0.110, bucket15=39) is True
    # new 15-min window -> record
    assert should_record_observation(last, date="2026-07-15", expiry="2026-07-17",
        short=743.0, long=733.0, ratio=0.104, bucket15=40) is True
    # strike change -> record
    assert should_record_observation(last, date="2026-07-15", expiry="2026-07-17",
        short=744.0, long=734.0, ratio=0.104, bucket15=39) is True
    # expiry change -> record
    assert should_record_observation(last, date="2026-07-15", expiry="2026-07-24",
        short=743.0, long=733.0, ratio=0.104, bucket15=39) is True
    # day rollover (same spread/ratio/window, new date) -> record: a stale prior-day anchor must not
    # suppress the first observation of the new day (keeps credit_obs_last from silencing a fresh day)
    assert should_record_observation(last, date="2026-07-16", expiry="2026-07-17",
        short=743.0, long=733.0, ratio=0.104, bucket15=39) is True
    # no prior observation -> record
    assert should_record_observation(None, date="2026-07-15", expiry="2026-07-17",
        short=743.0, long=733.0, ratio=0.104, bucket15=39) is True
