"""Conservative expected-executable credit + quote-quality guards (partner review v2 §4)."""


def package_mid(short_bid, short_ask, long_bid, long_ask):
    return (short_bid + short_ask) / 2.0 - (long_bid + long_ask) / 2.0


def package_natural(short_bid, short_ask, long_bid, long_ask):
    return short_bid - long_ask


def expected_executable_credit(short_bid, short_ask, long_bid, long_ask, expected_entry_slippage):
    return max(package_natural(short_bid, short_ask, long_bid, long_ask),
               package_mid(short_bid, short_ask, long_bid, long_ask) - expected_entry_slippage)


def quotes_valid(short_bid, short_ask, long_bid, long_ask):
    """False if any bid/ask missing (None) or crossed (bid>ask on a leg)."""
    vals = [short_bid, short_ask, long_bid, long_ask]
    if any(v is None for v in vals):
        return False
    return short_bid <= short_ask and long_bid <= long_ask


def package_too_wide(short_bid, short_ask, long_bid, long_ask, max_package_width_ratio):
    pm = package_mid(short_bid, short_ask, long_bid, long_ask)
    width = pm - package_natural(short_bid, short_ask, long_bid, long_ask)
    return width / max(pm, 0.01) > max_package_width_ratio


def spot_moved_too_far(signal_spot, order_spot, atr, max_move_atr):
    if atr <= 0:
        return False
    return abs(order_spot - signal_spot) / atr > max_move_atr
