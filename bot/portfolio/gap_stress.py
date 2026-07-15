"""Gap-risk stress test (partner review v2 §10): reprice spreads at SPY down 1/1.5/2 ATR."""


def stressed_spread_loss(short_strike, long_strike, credit, qty, spot, atr, drop_atr, wing_width=10.0):
    """Loss ($, positive = loss) on a bull put spread if SPY drops `drop_atr` ATRs, using a
    conservative intrinsic-value close estimate: stressed debit-to-close =
    min(wing_width, max(0, short-S) - max(0, long-S)) at S = spot - drop_atr*atr.
    Loss = (stressed_debit - credit) * 100 * qty  (negative means the position is still profitable)."""
    s = spot - drop_atr * atr
    stressed_debit = min(wing_width, max(0.0, short_strike - s) - max(0.0, long_strike - s))
    return (stressed_debit - credit) * 100.0 * qty


def gap_stress_losses(positions, spot, atr, wing_width=10.0):
    """positions: iterable of objects with .short_strike/.long_strike/.credit/.qty (open + proposed).
    Returns {1.0: total_loss, 1.5: total_loss, 2.0: total_loss} summed across all positions."""
    out = {}
    for d in (1.0, 1.5, 2.0):
        out[d] = round(sum(stressed_spread_loss(p.short_strike, p.long_strike, p.credit, p.qty,
                                                spot, atr, d, wing_width) for p in positions), 2)
    return out


def gap_stress_ok(positions, spot, atr, risk_equity, max_gap_stress_loss_pct, wing_width=10.0):
    """True if the 1.5-ATR total stressed loss does not exceed the budget."""
    return gap_stress_losses(positions, spot, atr, wing_width)[1.5] <= max_gap_stress_loss_pct * risk_equity
