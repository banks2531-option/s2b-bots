"""TDD for Task 2.1: conservative expected-executable credit + quote-quality guards
(partner review v2 §4)."""
import pytest

from bot.strategy.execution_price import (
    package_mid, package_natural, expected_executable_credit, quotes_valid,
    package_too_wide, spot_moved_too_far,
)


# ── package_mid / package_natural ────────────────────────────────────────────────────────────────

def test_package_mid_is_short_mid_minus_long_mid():
    # short mid = (3.40+3.50)/2 = 3.45; long mid = (1.60+1.70)/2 = 1.65
    assert package_mid(3.40, 3.50, 1.60, 1.70) == pytest.approx(1.80)


def test_package_natural_is_short_bid_minus_long_ask():
    assert package_natural(3.40, 3.50, 1.60, 1.70) == 1.70


# ── expected_executable_credit: conservative = max(natural, mid - slippage) ─────────────────────

def test_expected_executable_credit_uses_natural_when_quotes_have_zero_width():
    # zero-width quotes (bid==ask on both legs): natural == mid == 1.80; mid-slip (1.77) < natural
    # -> conservative credit falls back to natural (1.80), never allowing slippage to push it up.
    eec = expected_executable_credit(3.40, 3.40, 1.60, 1.60, 0.03)
    assert eec == pytest.approx(1.80)


def test_expected_executable_credit_uses_mid_minus_slippage_when_natural_is_lower():
    # wide package: short 3.00/5.00 (mid 4.00), long 0.00/0.10 (mid 0.05)
    # package_mid = 3.95; package_natural = 3.00-0.10 = 2.90; mid-slip(0.03) = 3.92 > natural
    eec = expected_executable_credit(3.00, 5.00, 0.00, 0.10, 0.03)
    assert eec == pytest.approx(3.92)


def test_expected_executable_credit_falls_back_to_natural_when_it_dominates():
    # degenerate case: mid-slip below natural -> natural wins
    eec = expected_executable_credit(3.40, 3.42, 1.60, 1.61, 0.03)
    natural = package_natural(3.40, 3.42, 1.60, 1.61)
    mid = package_mid(3.40, 3.42, 1.60, 1.61)
    assert eec == max(natural, mid - 0.03)
    assert eec == natural


# ── quotes_valid: missing / crossed detection ────────────────────────────────────────────────────

def test_quotes_valid_true_for_normal_quotes():
    assert quotes_valid(3.40, 3.50, 1.60, 1.70) is True


def test_quotes_valid_false_when_short_bid_missing():
    assert quotes_valid(None, 3.50, 1.60, 1.70) is False


def test_quotes_valid_false_when_any_leg_missing():
    assert quotes_valid(3.40, 3.50, None, 1.70) is False
    assert quotes_valid(3.40, 3.50, 1.60, None) is False
    assert quotes_valid(3.40, None, 1.60, 1.70) is False


def test_quotes_valid_false_when_short_leg_crossed():
    assert quotes_valid(3.50, 3.40, 1.60, 1.70) is False   # short bid > short ask


def test_quotes_valid_false_when_long_leg_crossed():
    assert quotes_valid(3.40, 3.50, 1.70, 1.60) is False   # long bid > long ask


def test_quotes_valid_true_when_bid_equals_ask():
    assert quotes_valid(3.40, 3.40, 1.60, 1.60) is True    # boundary: not crossed


# ── package_too_wide: width relative to mid vs MAX_PACKAGE_WIDTH_RATIO ───────────────────────────

def test_package_too_wide_true_for_wide_package():
    # mid=3.95, natural=2.90 -> width=1.05; ratio=1.05/3.95=0.2658 > 0.25
    assert package_too_wide(3.00, 5.00, 0.00, 0.10, 0.25) is True


def test_package_too_wide_false_for_tight_package():
    # mid=1.80, natural=1.70 -> width=0.10; ratio=0.10/1.80=0.0556 <= 0.25
    assert package_too_wide(3.40, 3.50, 1.60, 1.70, 0.25) is False


def test_package_too_wide_uses_floor_of_0_01_for_tiny_mid():
    # package_mid near zero must not divide-by-zero; denominator floors at 0.01
    assert package_too_wide(0.02, 0.02, 0.01, 0.01, 0.25) is False


# ── spot_moved_too_far ───────────────────────────────────────────────────────────────────────────

def test_spot_moved_too_far_false_within_threshold():
    # move = 0.5 on atr 6.0 -> 0.083 ATR <= 0.10
    assert spot_moved_too_far(575.0, 575.5, 6.0, 0.10) is False


def test_spot_moved_too_far_true_beyond_threshold():
    # move = 1.0 on atr 6.0 -> 0.167 ATR > 0.10
    assert spot_moved_too_far(575.0, 576.0, 6.0, 0.10) is True


def test_spot_moved_too_far_false_when_atr_nonpositive():
    assert spot_moved_too_far(575.0, 600.0, 0.0, 0.10) is False
    assert spot_moved_too_far(575.0, 600.0, -1.0, 0.10) is False


def test_spot_moved_too_far_uses_absolute_move():
    # a downward move must trigger the same as an upward move
    assert spot_moved_too_far(575.0, 574.0, 6.0, 0.10) is True
