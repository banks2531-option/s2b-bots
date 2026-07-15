from bot.strategy.credit_quality import credit_tier, dte_bucket, full_size_threshold


def test_reject_below_min():
    assert credit_tier(0.9, 10.0, min_ratio=0.10, full_threshold=0.115, probe_mult=0.40) == 0.0


def test_probe_band():
    assert credit_tier(1.10, 10.0, min_ratio=0.10, full_threshold=0.115, probe_mult=0.40) == 0.40


def test_full_size():
    assert credit_tier(1.20, 10.0, min_ratio=0.10, full_threshold=0.115, probe_mult=0.40) == 1.0


def test_dte_bucket():
    assert dte_bucket(4) == "4-5" and dte_bucket(7) == "6-8" and dte_bucket(10) == "9-11"


def test_threshold_uses_prior_only_and_floor():
    hist = [0.10, 0.11, 0.12, 0.13, 0.14]     # p40 ~ 0.118
    assert full_size_threshold(hist, floor=0.115) >= 0.115
    assert full_size_threshold([], floor=0.115) == 0.115   # empty -> floor
