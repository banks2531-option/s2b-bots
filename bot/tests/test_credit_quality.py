from bot.strategy.credit_quality import (
    classify_credit_quality,
    credit_tier,
    dte_bucket,
    full_size_threshold,
    low_credit_safety_pass,
)


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


# ── classify_credit_quality (§1): four lanes incl. new low_credit_safety band 0.08-0.10 ──────────

def test_classify_reject_below_absolute_floor():
    # 0.07 < ABSOLUTE_CREDIT_FLOOR (0.08) -> reject, no size, zero qty
    assert classify_credit_quality(0.07, 0.13) == ("reject", 0.0, 0)


def test_classify_low_credit_safety_band():
    # 0.08 <= 0.09 < STANDARD_PROBE_FLOOR (0.10) -> low_credit_safety, 0.25x, cap 1
    assert classify_credit_quality(0.09, 0.13) == ("low_credit_safety", 0.25, 1)


def test_classify_probe_below_adaptive_threshold():
    # STANDARD_PROBE_FLOOR (0.10) <= 0.12 < threshold 0.13 -> probe, 0.40x, no cap
    assert classify_credit_quality(0.12, 0.13) == ("probe", 0.40, None)


def test_classify_full_at_or_above_adaptive_threshold():
    # 0.14 >= threshold 0.13 -> full, 1.0x, no cap
    assert classify_credit_quality(0.14, 0.13) == ("full", 1.0, None)


def test_classify_boundary_absolute_floor_is_low_credit_not_reject():
    # exactly 0.08: NOT < 0.08, so it falls into low_credit_safety, not reject
    assert classify_credit_quality(0.08, 0.13) == ("low_credit_safety", 0.25, 1)


def test_classify_boundary_standard_probe_floor_is_not_low_credit():
    # exactly 0.10: NOT < 0.10, so it leaves the low_credit band -> probe (below threshold 0.13)
    assert classify_credit_quality(0.10, 0.13) == ("probe", 0.40, None)
    # exactly 0.10 with a threshold at/below it -> full, not low_credit_safety
    assert classify_credit_quality(0.10, 0.10) == ("full", 1.0, None)


# ── low_credit_safety_pass (§1): exact conjunction; flip each condition to its failing side ──────

def _passing_low_credit_inputs():
    return dict(
        target_to_cost_ratio=4.5,
        package_width_ratio=0.20,
        quote_age_seconds=2.0,
        cushion_atr=1.15,
        expected_move_cushion=0.90,
        short_delta=0.30,
        defensive_market_state=False,
    )


def test_low_credit_safety_pass_all_pass():
    assert low_credit_safety_pass(**_passing_low_credit_inputs()) is True


def test_low_credit_safety_pass_target_to_cost_fails():
    inp = _passing_low_credit_inputs()
    inp["target_to_cost_ratio"] = 4.4
    assert low_credit_safety_pass(**inp) is False


def test_low_credit_safety_pass_width_fails():
    inp = _passing_low_credit_inputs()
    inp["package_width_ratio"] = 0.21
    assert low_credit_safety_pass(**inp) is False


def test_low_credit_safety_pass_quote_age_fails():
    inp = _passing_low_credit_inputs()
    inp["quote_age_seconds"] = 2.1
    assert low_credit_safety_pass(**inp) is False


def test_low_credit_safety_pass_cushion_atr_fails():
    inp = _passing_low_credit_inputs()
    inp["cushion_atr"] = 1.14
    assert low_credit_safety_pass(**inp) is False


def test_low_credit_safety_pass_defensive_state_fails():
    inp = _passing_low_credit_inputs()
    inp["defensive_market_state"] = True
    assert low_credit_safety_pass(**inp) is False


def test_low_credit_safety_pass_or_branch_move_passes_delta_fails():
    # move cushion 0.95 (>=0.90) passes even though delta 0.40 (>0.30) fails -> True
    inp = _passing_low_credit_inputs()
    inp["expected_move_cushion"] = 0.95
    inp["short_delta"] = 0.40
    assert low_credit_safety_pass(**inp) is True


def test_low_credit_safety_pass_or_branch_delta_passes_move_fails():
    # delta 0.25 (<=0.30) passes even though move cushion 0.80 (<0.90) fails -> True
    inp = _passing_low_credit_inputs()
    inp["expected_move_cushion"] = 0.80
    inp["short_delta"] = 0.25
    assert low_credit_safety_pass(**inp) is True


def test_low_credit_safety_pass_or_branch_both_fail():
    # both sides of the OR fail: move 0.80 (<0.90) and delta 0.40 (>0.30) -> False
    inp = _passing_low_credit_inputs()
    inp["expected_move_cushion"] = 0.80
    inp["short_delta"] = 0.40
    assert low_credit_safety_pass(**inp) is False
